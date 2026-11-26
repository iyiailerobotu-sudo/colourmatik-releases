/*
	colourMatik.cpp — applies a colourMatik 3D LUT (per-instance "slot" file)
	with a built-in Intensity slider. by Sevki Bugra Ozbek - catheadai.com

	Premiere Pro: renders in 32-bit float BGRA (declared via the Premiere pixel
	format suite; 8-bit BGRA fallback), so the engine's 65^3 LUT is applied with
	zero quantisation — the clip gets exactly what the engine computed.
	After Effects (CM_AE_BUILD): a SmartFX effect rendering at the project's own
	depth - 8, 16 or 32 bits per channel - with the iterate suites. Before 1.2 it
	was 8-bit only, so AE squeezed every 16/32-bit project down to 8 bits for it
	(banding in skies, clipped highlights). The engine's LUT expects Rec.709 /
	sRGB-encoded values; in a linearised working space ("Linear Working Space",
	set by the panel from the project) pixels are encoded first and decoded after.
*/

#include "colourMatik.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <stdarg.h>
#include <stddef.h>   // ptrdiff_t (signed row-stride arithmetic)
// Thread-local storage keyword. Each render thread owns its LUT (see the
// per-thread cache below), so there is no shared state and no lock to orphan.
#ifdef AE_OS_WIN
#define CM_TLS __declspec(thread)
#else
#define CM_TLS __thread
#endif

// ------------------------------------------------------------ LUT helpers
static void cm_lut_path(A_long slot, char *out, size_t n) {
#ifdef AE_OS_WIN
	const char *base = getenv("APPDATA");            // %USERPROFILE%\AppData\Roaming
	if (!base) base = "";
	snprintf(out, n, "%s\\colourMatik\\slot_%ld.cube", base, (long)slot);
#else
	const char *home = getenv("HOME");
	if (!home) home = "";
	snprintf(out, n, "%s/Library/Application Support/colourMatik/slot_%ld.cube", home, (long)slot);
#endif
}

// Load a .cube (RED varies fastest, edge size 2..CM_LUT_MAX) into 'lut', a caller
// buffer of CM_LUT_N3MAX*3 floats. Returns 1 and sets *out_size on success.
static int cm_load_lut_buf(A_long slot, float *lut, int *out_size) {
	char path[1024];
	cm_lut_path(slot, path, sizeof(path));
	FILE *f = fopen(path, "r");
	if (!f) return 0;
	int size = 0, count = 0;
	char line[512];
	while (fgets(line, sizeof(line), f)) {
		if (line[0] == '#' || line[0] == '\r' || line[0] == '\n') continue;
		if (strncmp(line, "LUT_3D_SIZE", 11) == 0) { sscanf(line + 11, "%d", &size); continue; }
		if (strncmp(line, "TITLE", 5) == 0 || strncmp(line, "DOMAIN", 6) == 0 ||
			strncmp(line, "LUT_1D", 6) == 0) continue;
		float r, g, b;
		if (sscanf(line, "%f %f %f", &r, &g, &b) == 3) {
			// a corrupt file can carry nan/inf ("nan" parses as a float!) — one bad
			// node would smear NaN across every interpolated pixel. Reject the file.
			if (!isfinite(r) || !isfinite(g) || !isfinite(b)) { fclose(f); return 0; }
			if (count < CM_LUT_N3MAX) {
				lut[count * 3 + 0] = r;
				lut[count * 3 + 1] = g;
				lut[count * 3 + 2] = b;
			}
			count++;
		}
	}
	fclose(f);
	if (size < 2 || size > CM_LUT_MAX || count < size * size * size) return 0;
	*out_size = size;
	return 1;
}

// ---- Per-thread LUT cache (MFR-safe, lock-free) ----------------------------
// After Effects Multi-Frame Rendering runs Render on WORKER THREADS where
// in_data->sequence_data is NULL — so per-instance sequence data can't carry the
// LUT (that made the AE effect render identity: it read a NULL handle and fell
// through to passthrough). Each render THREAD instead keeps its own LUT in
// thread-local storage, so nothing is shared and there is NO lock. That matters
// because Premiere force-terminates superseded render threads mid-flight: a
// thread killed here just drops its own TLS — there is no global lock to orphan
// (the old os_unfair_lock whole-process abort / a plain-mutex permanent deadlock)
// and no shared entry to poison. The engine writes each match to a fresh
// monotonic slot and the .cube is immutable, so a thread reloads only when it
// meets a slot it isn't already holding.
//
// Cost: buffers (~3 MB) are allocated lazily — a thread that only ever renders one
// slot keeps exactly one, and a thread that never renders a real slot keeps none —
// but they are never freed, so they cost one buffer per (thread, slot) for as long
// as the host keeps that thread. A host that recycles its render pool therefore
// leaves a buffer behind per retired thread. That is the deliberate trade: the
// alternative, one shared copy behind a lock, is what a force-terminated Premiere
// render thread can strand — orphaning the lock across the slow 65^3 load and
// wedging every later render (see 8f6cfc3). Bounded, unshared memory beats a
// process-wide hang.
// A few slots per thread, not one: a comp can stack several graded layers, and a
// worker thread renders them all for a frame. With a single buffer those layers
// would fight over it and re-parse the whole 3 MB .cube on every render call.
#define CM_TLS_SLOTS 4
typedef struct {
	A_long  slot;   // slot held in 'lut' — only meaningful while ok
	int     size;   // LUT edge length (e.g. 65)
	int     ok;     // 1 = 'lut' holds a valid LUT for 'slot'
	float  *lut;    // malloc'd CM_LUT_N3MAX*3 floats on first use; reused after
} CMTlsLut;
static CM_TLS CMTlsLut g_tls[CM_TLS_SLOTS];   // zero-init: ok=0, lut=NULL
static CM_TLS int      g_tls_victim = 0;      // round-robin when all entries are live

// Hand back a read-only LUT for 'slot'. Returns 1 and sets *out_lut / *out_size
// when a valid LUT is available; 0 -> caller renders identity.
static int cm_get_lut(A_long slot, const float **out_lut, int *out_size) {
	// Slot 0 is the param default — "nothing matched yet", and the engine never
	// writes slot_0.cube (its counter starts at 1). Answer that from here: a freshly
	// applied instance would otherwise allocate a 3 MB buffer and re-attempt a
	// doomed fopen on every single frame until the panel sets a real slot.
	if (slot <= 0) return 0;

	// Already loaded on this thread? (keyed on ok AND slot, so a failed load of
	// some other slot can never make a good one look stale)
	for (int i = 0; i < CM_TLS_SLOTS; i++) {
		if (g_tls[i].ok && g_tls[i].slot == slot) {
			*out_lut = g_tls[i].lut; *out_size = g_tls[i].size; return 1;
		}
	}
	// Take a free entry if there is one, else evict round-robin. Eviction is safe:
	// the buffer belongs to this thread alone and no other thread can be reading it.
	CMTlsLut *e = NULL;
	for (int i = 0; i < CM_TLS_SLOTS; i++) if (!g_tls[i].ok) { e = &g_tls[i]; break; }
	if (!e) { e = &g_tls[g_tls_victim]; g_tls_victim = (g_tls_victim + 1) % CM_TLS_SLOTS; }

	if (!e->lut) e->lut = (float *)malloc(sizeof(float) * (size_t)CM_LUT_N3MAX * 3);
	int sz = 0;
	e->ok   = (e->lut != NULL) && cm_load_lut_buf(slot, e->lut, &sz);
	e->size = e->ok ? sz : 0;
	// Claim the slot ONLY on success: a failed load (a .cube still syncing in from
	// Dropbox/iCloud, briefly held by an AV scanner, or arriving a moment after the
	// panel set the param) leaves the entry free, so the next render retries and a
	// transient failure never pins the slot to identity for the session.
	e->slot = e->ok ? slot : -1;
	if (e->ok) { *out_lut = e->lut; *out_size = e->size; return 1; }
	return 0;
}

static inline float cm_clampf(float v) { return v < 0.f ? 0.f : (v > 1.f ? 1.f : v); }

// Per-pixel hash noise in [-0.5, 0.5). Used to jitter the 8-bit INPUT by one
// LSB before sampling the LUT: a strong match stretches dark gradients (night
// skies), turning 8-bit steps into visible bands/posterisation ("moire").
// Jittering the input breaks those contours into fine, film-like grain. The
// same value is used for R,G,B (luma-correlated) so it never adds colour noise.
static inline float cm_hash_noise(A_long x, A_long y) {
	unsigned h = (unsigned)x * 73856093u ^ (unsigned)y * 19349663u;
	h = (h ^ (h >> 13)) * 1274126177u;
	return (float)((h >> 8) & 0xFFFF) / 65535.0f - 0.5f;
}

// Trilinear sample of an S^3 LUT at (r,g,b) in [0,1]. idx = r + g*S + b*S*S.
// S must be >= 2 (callers only sample a fully published LUT, but keep the guard).
static void cm_sample(const float *lut, int S, float r, float g, float b, float out[3]) {
	if (S < 2) { out[0] = r; out[1] = g; out[2] = b; return; }
	float cr = cm_clampf(r) * (S - 1), cg = cm_clampf(g) * (S - 1), cb = cm_clampf(b) * (S - 1);
	int r0 = (int)cr, g0 = (int)cg, b0 = (int)cb;
	if (r0 > S - 2) r0 = S - 2; if (r0 < 0) r0 = 0;
	if (g0 > S - 2) g0 = S - 2; if (g0 < 0) g0 = 0;
	if (b0 > S - 2) b0 = S - 2; if (b0 < 0) b0 = 0;
	float fr = cr - r0, fg = cg - g0, fb = cb - b0;
	out[0] = out[1] = out[2] = 0.f;
	for (int c = 0; c < 8; c++) {
		int dr = c & 1, dg = (c >> 1) & 1, db = (c >> 2) & 1;
		float w = (dr ? fr : 1 - fr) * (dg ? fg : 1 - fg) * (db ? fb : 1 - fb);
		int idx = ((r0 + dr) + (g0 + dg) * S + (b0 + db) * S * S) * 3;
		out[0] += w * lut[idx + 0];
		out[1] += w * lut[idx + 1];
		out[2] += w * lut[idx + 2];
	}
}

typedef struct { const float *lut; int size; float t; } CM_Info;

// Diagnostic trace -> /tmp/colourmatik_fx.log (or %TEMP% on Windows), capped.
// DORMANT unless the COLOURMATIK_TRACE env var is set, so shipping builds are
// silent but field-debuggable. Premiere's "a low-level exception occurred"
// doesn't say where; this pinpoints the exact render call when enabled.
#ifdef AE_OS_WIN
#define CM_TRACE_MARK "C:\\colourmatik_trace_on"
#define CM_TRACE_LOG  "C:\\colourmatik_fx.log"
#else
#define CM_TRACE_MARK "/tmp/colourmatik_trace_on"
#define CM_TRACE_LOG  "/tmp/colourmatik_fx.log"
#endif
static void cm_trace(const char *fmtstr, ...) {
	// Gated on a MARKER FILE (not an env var — Premiere's render context doesn't
	// inherit launchctl env). Absent on users' machines, so shipping is silent.
	static int enabled = -1;
	if (enabled == -1) { FILE *m = fopen(CM_TRACE_MARK, "r"); enabled = m ? 1 : 0; if (m) fclose(m); }
	if (!enabled) return;
	static int n = 0;
	if (n > 400) return;
	n++;
	FILE *lf = fopen(CM_TRACE_LOG, "a");
	if (!lf) return;
	va_list ap;
	va_start(ap, fmtstr);
	vfprintf(lf, fmtstr, ap);
	va_end(ap);
	fputc('\n', lf);
	fclose(lf);
}

// ------------------------------------------------------------ AE 8-bit ARGB path
static PF_Err cm_pixel(void *refcon, A_long x, A_long y, PF_Pixel *inP, PF_Pixel *outP) {
	CM_Info *info = (CM_Info *)refcon;
	float d = cm_hash_noise(x, y) / 255.f;   // anti-banding input jitter
	float r = cm_clampf(inP->red / 255.f + d);
	float g = cm_clampf(inP->green / 255.f + d);
	float b = cm_clampf(inP->blue / 255.f + d);
	float o[3];
	cm_sample(info->lut, info->size, r, g, b, o);
	float t = info->t;
	outP->alpha = inP->alpha;
	outP->red   = (A_u_char)(cm_clampf(r + t * (o[0] - r)) * 255.f + 0.5f);
	outP->green = (A_u_char)(cm_clampf(g + t * (o[1] - g)) * 255.f + 0.5f);
	outP->blue  = (A_u_char)(cm_clampf(b + t * (o[2] - b)) * 255.f + 0.5f);
	return PF_Err_NONE;
}

#ifdef CM_AE_BUILD
// ------------------------------------------------------------ AE deep colour
typedef struct { const float *lut; int size; float t; int linear; } CM_AEInfo;

// sRGB transfer (the engine's "Standard (Rec.709 / sRGB)" encoding), for a
// linearised working space. Below the toe both are linear, so negative and tiny
// values pass straight through; above white the power law carries on.
static inline float cm_enc(float v) { return v <= 0.0031308f ? v * 12.92f : 1.055f * powf(v, 1.0f / 2.4f) - 0.055f; }
static inline float cm_dec(float v) { return v <= 0.04045f ? v / 12.92f : powf((v + 0.055f) / 1.055f, 2.4f); }

// One pixel in encoded floats: sample at the clamped point, blend from it, so
// over-range values (superwhites in 32-bit) keep their offset - same rule as the
// Premiere float path.
static inline void cm_ae_apply(const CM_AEInfo *info, float r, float g, float b, float o[3]) {
	if (info->linear) { r = cm_enc(r); g = cm_enc(g); b = cm_enc(b); }
	if (info->lut) {
		float rc = cm_clampf(r), gc = cm_clampf(g), bc = cm_clampf(b), s[3];
		cm_sample(info->lut, info->size, rc, gc, bc, s);
		r += info->t * (s[0] - rc); g += info->t * (s[1] - gc); b += info->t * (s[2] - bc);
	}
	if (info->linear) { r = cm_dec(r); g = cm_dec(g); b = cm_dec(b); }
	o[0] = r; o[1] = g; o[2] = b;
}

static PF_Err cm_ae_pix8(void *refcon, A_long x, A_long y, PF_Pixel8 *inP, PF_Pixel8 *outP) {
	const CM_AEInfo *info = (const CM_AEInfo *)refcon;
	float d = cm_hash_noise(x, y) / 255.f;   // anti-banding input jitter, as before
	float o[3];
	cm_ae_apply(info, cm_clampf(inP->red / 255.f + d), cm_clampf(inP->green / 255.f + d),
	            cm_clampf(inP->blue / 255.f + d), o);
	outP->alpha = inP->alpha;
	outP->red   = (A_u_char)(cm_clampf(o[0]) * 255.f + 0.5f);
	outP->green = (A_u_char)(cm_clampf(o[1]) * 255.f + 0.5f);
	outP->blue  = (A_u_char)(cm_clampf(o[2]) * 255.f + 0.5f);
	return PF_Err_NONE;
}

static PF_Err cm_ae_pix16(void *refcon, A_long x, A_long y, PF_Pixel16 *inP, PF_Pixel16 *outP) {
	const CM_AEInfo *info = (const CM_AEInfo *)refcon;
	const float k = 1.f / PF_MAX_CHAN16;      // AE's 16-bit white is 32768
	float o[3];
	cm_ae_apply(info, inP->red * k, inP->green * k, inP->blue * k, o);
	outP->alpha = inP->alpha;
	outP->red   = (A_u_short)(cm_clampf(o[0]) * PF_MAX_CHAN16 + 0.5f);
	outP->green = (A_u_short)(cm_clampf(o[1]) * PF_MAX_CHAN16 + 0.5f);
	outP->blue  = (A_u_short)(cm_clampf(o[2]) * PF_MAX_CHAN16 + 0.5f);
	return PF_Err_NONE;
}

static PF_Err cm_ae_pixf(void *refcon, A_long x, A_long y, PF_PixelFloat *inP, PF_PixelFloat *outP) {
	const CM_AEInfo *info = (const CM_AEInfo *)refcon;
	float o[3];
	cm_ae_apply(info, inP->red, inP->green, inP->blue, o);
	outP->alpha = inP->alpha;
	outP->red = o[0]; outP->green = o[1]; outP->blue = o[2];   // 32-bit: no clamp
	return PF_Err_NONE;
}

static PF_Err PreRender(PF_InData *in_data, PF_OutData *out_data, PF_PreRenderExtra *extra) {
	PF_Err err = PF_Err_NONE;
	PF_RenderRequest req = extra->input->output_request;
	req.preserve_rgb_of_zero_alpha = TRUE;    // a colour operation: never invent RGB under A=0
	PF_CheckoutResult in_result;
	AEFX_CLR_STRUCT(in_result);
	ERR(extra->cb->checkout_layer(in_data->effect_ref, CM_INPUT, CM_INPUT, &req,
	                              in_data->current_time, in_data->time_step, in_data->time_scale, &in_result));
	if (!err) {
		extra->output->result_rect = in_result.result_rect;
		extra->output->max_result_rect = in_result.max_result_rect;
		extra->output->solid = in_result.solid;
	}
	return err;
}

static PF_Err SmartRender(PF_InData *in_data, PF_OutData *out_data, PF_SmartRenderExtra *extra) {
	PF_Err err = PF_Err_NONE, err2 = PF_Err_NONE;
	PF_ParamDef p_int, p_slot, p_lin;
	AEFX_CLR_STRUCT(p_int); AEFX_CLR_STRUCT(p_slot); AEFX_CLR_STRUCT(p_lin);
	PF_EffectWorld *input = NULL, *output = NULL;
	ERR(PF_CHECKOUT_PARAM(in_data, CM_INTENSITY, in_data->current_time, in_data->time_step, in_data->time_scale, &p_int));
	ERR(PF_CHECKOUT_PARAM(in_data, CM_SLOT, in_data->current_time, in_data->time_step, in_data->time_scale, &p_slot));
	ERR(PF_CHECKOUT_PARAM(in_data, CM_LINEAR, in_data->current_time, in_data->time_step, in_data->time_scale, &p_lin));
	ERR(extra->cb->checkout_layer_pixels(in_data->effect_ref, CM_INPUT, &input));
	ERR(extra->cb->checkout_output(in_data->effect_ref, &output));
	if (!err && input && output) {
		CM_AEInfo info;
		info.t = (float)(p_int.u.fs_d.value / 100.0);
		info.linear = p_lin.u.bd.value ? 1 : 0;
		info.lut = NULL; info.size = 0;
		A_long slot = (A_long)(p_slot.u.fs_d.value + 0.5);
		const float *lut = NULL; int lut_size = 0;
		if (info.t != 0.f && cm_get_lut(slot, &lut, &lut_size) && lut_size >= 2) { info.lut = lut; info.size = lut_size; }
		int depth = extra->input->bitdepth;
		cm_trace("AE smart render slot=%ld size=%d bpc=%d linear=%d t=%.2f", (long)slot, info.size, depth, info.linear, info.t);
		SPBasicSuite *bs = in_data->pica_basicP;
		if (depth == 32) {
			PF_IterateFloatSuite2 *it = NULL;
			if (bs->AcquireSuite(kPFIterateFloatSuite, kPFIterateFloatSuiteVersion2, (const void **)&it) == kSPNoError && it) {
				err = it->iterate(in_data, 0, output->height, input, NULL, (void *)&info, cm_ae_pixf, output);
				bs->ReleaseSuite(kPFIterateFloatSuite, kPFIterateFloatSuiteVersion2);
			} else err = PF_Err_BAD_CALLBACK_PARAM;
		} else if (depth == 16) {
			PF_iterate16Suite2 *it = NULL;
			if (bs->AcquireSuite(kPFIterate16Suite, kPFIterate16SuiteVersion2, (const void **)&it) == kSPNoError && it) {
				err = it->iterate(in_data, 0, output->height, input, NULL, (void *)&info, cm_ae_pix16, output);
				bs->ReleaseSuite(kPFIterate16Suite, kPFIterate16SuiteVersion2);
			} else err = PF_Err_BAD_CALLBACK_PARAM;
		} else {
			PF_Iterate8Suite2 *it = NULL;
			if (bs->AcquireSuite(kPFIterate8Suite, kPFIterate8SuiteVersion2, (const void **)&it) == kSPNoError && it) {
				err = it->iterate(in_data, 0, output->height, input, NULL, (void *)&info, cm_ae_pix8, output);
				bs->ReleaseSuite(kPFIterate8Suite, kPFIterate8SuiteVersion2);
			} else err = PF_Err_BAD_CALLBACK_PARAM;
		}
	}
	ERR2(PF_CHECKIN_PARAM(in_data, &p_int));
	ERR2(PF_CHECKIN_PARAM(in_data, &p_slot));
	ERR2(PF_CHECKIN_PARAM(in_data, &p_lin));
	return err;
}
#endif  // CM_AE_BUILD

// ------------------------------------------------------------ Premiere BGRA paths
// Row loops over the effect worlds; memory order is B,G,R,A per pixel.
// IMPORTANT: honour the host's abort callback every few rows. Premiere cancels
// superseded renders (project open, scrubbing, apply-time thumbnails); a plugin
// that never checks gets hard-cancelled — which surfaces in the Events panel as
// "a low-level exception occurred". Checking lets us exit cleanly instead.
#define CM_ABORT_EVERY 16
static int cm_aborted(PF_InData *in_data, A_long y) {
	// DISABLED under Premiere: calling in_data->inter.abort in a Premiere software
	// render faults (Premiere doesn't populate the AE interaction callbacks the
	// same way). Premiere cancels superseded renders itself, so we don't need it.
	(void)in_data; (void)y;
	return 0;
}

// Row base pointer. rowbytes is SIGNED and often NEGATIVE in Premiere (bottom-up
// frames, esp. thumbnails / load-time renders): data points at the first output
// row and the stride walks BACKWARD through memory. The multiply MUST stay in
// signed 64-bit — casting a negative stride through size_t makes a huge positive
// offset and walks off the buffer (SIGSEGV that Premiere logs as a "low-level
// exception"). ptrdiff_t keeps it signed and 64-bit on both macOS and Win64.
#define CM_ROW(base, y, rb) ((char *)(base) + (ptrdiff_t)(y) * (ptrdiff_t)(rb))

static PF_Err cm_render_bgra_32f(PF_InData *in_data, PF_EffectWorld *src, PF_EffectWorld *dst,
                                 A_long width, A_long height, CM_Info *info) {
	const float *lut = info->lut;
	const int S = info->size;
	const float t = info->t;
	for (A_long y = 0; y < height; y++) {
		if (cm_aborted(in_data, y)) return PF_Interrupt_CANCEL;
		const float *ip = (const float *)CM_ROW(src->data, y, src->rowbytes);
		float *op = (float *)CM_ROW(dst->data, y, dst->rowbytes);
		for (A_long x = 0; x < width; x++) {
			float b = ip[0], g = ip[1], r = ip[2], a = ip[3];
			float rc = cm_clampf(r), gc = cm_clampf(g), bc = cm_clampf(b);
			float o[3];
			cm_sample(lut, S, rc, gc, bc, o);
			// blend from the CLAMPED sample point, so over-range (HDR) offsets survive
			op[0] = b + t * (o[2] - bc);
			op[1] = g + t * (o[1] - gc);
			op[2] = r + t * (o[0] - rc);
			op[3] = a;
			ip += 4; op += 4;
		}
	}
	return PF_Err_NONE;
}

static PF_Err cm_render_bgra_8u(PF_InData *in_data, PF_EffectWorld *src, PF_EffectWorld *dst,
                                A_long width, A_long height, CM_Info *info) {
	const float *lut = info->lut;
	const int S = info->size;
	const float t = info->t;
	for (A_long y = 0; y < height; y++) {
		if (cm_aborted(in_data, y)) return PF_Interrupt_CANCEL;
		const unsigned char *ip = (const unsigned char *)CM_ROW(src->data, y, src->rowbytes);
		unsigned char *op = (unsigned char *)CM_ROW(dst->data, y, dst->rowbytes);
		for (A_long x = 0; x < width; x++) {
			// one-LSB input jitter: kills banding/posterisation on stretched gradients
			float d = cm_hash_noise(x, y) / 255.f;
			float b = cm_clampf(ip[0] / 255.f + d);
			float g = cm_clampf(ip[1] / 255.f + d);
			float r = cm_clampf(ip[2] / 255.f + d);
			float o[3];
			cm_sample(lut, S, r, g, b, o);
			op[0] = (unsigned char)(cm_clampf(b + t * (o[2] - b)) * 255.f + 0.5f);
			op[1] = (unsigned char)(cm_clampf(g + t * (o[1] - g)) * 255.f + 0.5f);
			op[2] = (unsigned char)(cm_clampf(r + t * (o[0] - r)) * 255.f + 0.5f);
			op[3] = ip[3];
			ip += 4; op += 4;
		}
	}
	return PF_Err_NONE;
}

static PF_Err cm_copy_rows(PF_InData *in_data, PF_EffectWorld *src, PF_EffectWorld *dst,
                           A_long width, A_long height, size_t bpp) {
	size_t nbytes = (size_t)width * bpp;
	for (A_long y = 0; y < height; y++) {
		if (cm_aborted(in_data, y)) return PF_Interrupt_CANCEL;
		memcpy(CM_ROW(dst->data, y, dst->rowbytes),
		       CM_ROW(src->data, y, src->rowbytes), nbytes);
	}
	return PF_Err_NONE;
}

// ------------------------------------------------------------ AE entry points
static PF_Err About(PF_InData *in_data, PF_OutData *out_data, PF_ParamDef *params[], PF_LayerDef *output) {
	PF_SPRINTF(out_data->return_msg, "%s v%d.%d\r%s", NAME, MAJOR_VERSION, MINOR_VERSION, DESCRIPTION);
	return PF_Err_NONE;
}

static PF_Err GlobalSetup(PF_InData *in_data, PF_OutData *out_data, PF_ParamDef *params[], PF_LayerDef *output) {
	out_data->my_version = PF_VERSION(MAJOR_VERSION, MINOR_VERSION, BUG_VERSION, STAGE_VERSION, BUILD_VERSION);
	out_data->out_flags |= PF_OutFlag_PIX_INDEPENDENT | PF_OutFlag_USE_OUTPUT_EXTENT;
	// Multi-Frame Rendering: AE renders many frames on parallel worker threads. We
	// keep NO sequence data — the LUT lives in a per-thread, lock-free cache (see
	// cm_get_lut), each thread loading its own copy from the immutable slot_<N>.cube.
	// There is no shared mutable state across threads, so declaring MFR support is
	// safe and silences AE's "not optimized for Multi-Frame Rendering" warning. We
	// hold no sequence data, so SEQUENCE_DATA_NEEDS_FLATTENING / flattening callbacks
	// are irrelevant.
	out_data->out_flags2 |= PF_OutFlag2_SUPPORTS_THREADED_RENDERING;
#ifdef CM_AE_BUILD
	// After Effects: render at the project's depth (must match the PiPL)
	out_data->out_flags  |= PF_OutFlag_DEEP_COLOR_AWARE;
	out_data->out_flags2 |= PF_OutFlag2_SUPPORTS_SMART_RENDER | PF_OutFlag2_FLOAT_COLOR_AWARE;
#endif

	// Premiere: declare high-precision pixel formats (float preferred, 8-bit fallback).
	if (in_data->appl_id == kAppID_Premiere && in_data->pica_basicP) {
		SPBasicSuite *bs = in_data->pica_basicP;
		PF_PixelFormatSuite1 *pfs = NULL;
		if (bs->AcquireSuite(kPFPixelFormatSuite, kPFPixelFormatSuiteVersion1,
		                     (const void **)&pfs) == kSPNoError && pfs) {
			(*pfs->ClearSupportedPixelFormats)(in_data->effect_ref);
			(*pfs->AddSupportedPixelFormat)(in_data->effect_ref, PrPixelFormat_BGRA_4444_32f);
			(*pfs->AddSupportedPixelFormat)(in_data->effect_ref, PrPixelFormat_BGRA_4444_8u);
			bs->ReleaseSuite(kPFPixelFormatSuite, kPFPixelFormatSuiteVersion1);
		}
	}
	return PF_Err_NONE;
}

static PF_Err ParamsSetup(PF_InData *in_data, PF_OutData *out_data, PF_ParamDef *params[], PF_LayerDef *output) {
	PF_ParamDef def;

	AEFX_CLR_STRUCT(def);
	PF_ADD_FLOAT_SLIDERX("Intensity", CM_INTENSITY_MIN, CM_INTENSITY_MAX, CM_INTENSITY_MIN,
		CM_INTENSITY_MAX, CM_INTENSITY_DFLT, PF_Precision_INTEGER, 0, 0, CM_INTENSITY_DISK_ID);

	AEFX_CLR_STRUCT(def);
	PF_ADD_FLOAT_SLIDERX("Match Slot", CM_SLOT_MIN, CM_SLOT_MAX, CM_SLOT_MIN,
		CM_SLOT_MAX, CM_SLOT_DFLT, PF_Precision_INTEGER, 0, 0, CM_SLOT_DISK_ID);

#ifdef CM_AE_BUILD
	// The engine's LUT expects Rec.709 / sRGB-encoded values. The panel ticks this
	// from the project's "Linearize Working Space" when it applies the effect.
	AEFX_CLR_STRUCT(def);
	PF_ADD_CHECKBOXX("Linear Working Space", FALSE, 0, CM_LINEAR_DISK_ID);
#endif

	out_data->num_params = CM_NUM_PARAMS;
	return PF_Err_NONE;
}

// The LUT no longer lives in sequence data (it's in the per-thread cache), so
// these are minimal: we keep NO per-instance sequence data at all. Disposing any
// handle a saved project restored also sidesteps the old load-time crash where a
// flattened struct carried a garbage 'lut' pointer from another build.
static PF_Err SequenceSetup(PF_InData *in_data, PF_OutData *out_data, PF_ParamDef *params[], PF_LayerDef *output) {
	out_data->sequence_data = NULL;
	return PF_Err_NONE;
}

static PF_Err SequenceSetdown(PF_InData *in_data, PF_OutData *out_data, PF_ParamDef *params[], PF_LayerDef *output) {
	if (in_data->sequence_data) PF_DISPOSE_HANDLE(in_data->sequence_data);
	out_data->sequence_data = NULL;
	return PF_Err_NONE;
}

static PF_Err SequenceResetup(PF_InData *in_data, PF_OutData *out_data, PF_ParamDef *params[], PF_LayerDef *output) {
	if (in_data->sequence_data) PF_DISPOSE_HANDLE(in_data->sequence_data);
	out_data->sequence_data = NULL;
	return PF_Err_NONE;
}

static PF_Err Render(PF_InData *in_data, PF_OutData *out_data, PF_ParamDef *params[], PF_LayerDef *output) {
	PF_Err err = PF_Err_NONE;

	float intensity = (float)(params[CM_INTENSITY]->u.fs_d.value / 100.0);
	A_long slot = (A_long)(params[CM_SLOT]->u.fs_d.value + 0.5);
	PF_EffectWorld *inputW = &params[CM_INPUT]->u.ld;
	int identity = 0;

	// The LUT comes from this thread's own slot-keyed cache — NOT sequence data.
	// Under After Effects Multi-Frame Rendering, Render runs on worker threads
	// where in_data->sequence_data is NULL, so any LUT stashed there is invisible
	// (that was the "renders identity in AE" bug). Every thread loads its own copy
	// from the immutable .cube, so Premiere and AE (MFR or not) both get a real LUT
	// with no shared state to synchronise.
	const float *lut = NULL;
	int lut_size = 0;
	// At zero intensity the render is identity whatever the LUT says, so don't even
	// look it up — a dialled-out instance never pays for a .cube parse.
	int have = (intensity != 0.f) && cm_get_lut(slot, &lut, &lut_size);
	if (!have || lut_size < 2)
		identity = 1;
	cm_trace("Render enter appl=0x%x slot=%ld have=%d size=%d identity=%d",
	         (unsigned)in_data->appl_id, (long)slot, have, lut_size, identity);

	CM_Info info; info.lut = lut; info.size = lut_size; info.t = intensity;

	// ---------------- Premiere Pro: BGRA float / 8-bit paths ----------------
	// Gate on appl_id ALONE. During load-time / thumbnail renders pica_basicP can
	// be NULL; if we fell through to the AE PF_ITERATE/PF_COPY path on a Premiere
	// world we'd fault (that's the load-time "low-level exception"). A Premiere
	// render is ALWAYS handled here — the pixel format is inferred from the row
	// stride when the suite is unavailable.
	if (in_data->appl_id == kAppID_Premiere) {
		PrPixelFormat fmt = PrPixelFormat_BGRA_4444_8u;
		int fmt_ok = 0;
		if (in_data->pica_basicP) {
			SPBasicSuite *bs = in_data->pica_basicP;
			PF_PixelFormatSuite1 *pfs = NULL;
			if (bs->AcquireSuite(kPFPixelFormatSuite, kPFPixelFormatSuiteVersion1,
			                     (const void **)&pfs) == kSPNoError && pfs) {
				fmt_ok = ((*pfs->GetPixelFormat)(output, &fmt) == PF_Err_NONE);
				bs->ReleaseSuite(kPFPixelFormatSuite, kPFPixelFormatSuiteVersion1);
			}
		}
		// On transient apply-time renders GetPixelFormat can fail or return
		// garbage. We only ever declared the two BGRA formats, so infer from the
		// row stride — the one reliable signal (|rowbytes|/width: 4 -> 8u, 16 -> 32f).
		if (!fmt_ok || (fmt != PrPixelFormat_BGRA_4444_32f &&
		                fmt != PrPixelFormat_BGRA_4444_8u)) {
			A_long rb = output->rowbytes < 0 ? -output->rowbytes : output->rowbytes;
			A_long px = (output->width > 0) ? rb / output->width : 4;
			fmt = (px >= 16) ? PrPixelFormat_BGRA_4444_32f : PrPixelFormat_BGRA_4444_8u;
			cm_trace("  (fmt query unreliable -> inferred %s from stride %ld/px)",
			         px >= 16 ? "32f" : "8u", (long)px);
		}
		size_t bpp = (fmt == PrPixelFormat_BGRA_4444_32f) ? 16 : 4;
		A_long w = MIN(inputW->width, output->width);
		A_long h = MIN(inputW->height, output->height);
		// Never trust dims past what the rowbytes can actually hold, and never
		// touch a world without pixels (half-initialised apply-time worlds).
		{
			A_long irb = inputW->rowbytes < 0 ? -inputW->rowbytes : inputW->rowbytes;
			A_long orb = output->rowbytes < 0 ? -output->rowbytes : output->rowbytes;
			if (irb > 0) w = MIN(w, (A_long)(irb / (A_long)bpp));
			if (orb > 0) w = MIN(w, (A_long)(orb / (A_long)bpp));
		}
		cm_trace("render fmt=%d in=%ldx%ld rb=%ld %p out=%ldx%ld rb=%ld %p slot=%ld size=%d id=%d t=%.2f",
		         (int)fmt, (long)inputW->width, (long)inputW->height, (long)inputW->rowbytes,
		         inputW->data, (long)output->width, (long)output->height, (long)output->rowbytes,
		         output->data, (long)slot, lut_size, identity, intensity);
		if (!inputW->data || !output->data || w <= 0 || h <= 0) {
			cm_trace("  -> skipped (empty/null world)");
			return PF_Err_NONE;
		}
		PF_Err perr;
		if (fmt == PrPixelFormat_BGRA_4444_32f) {
			perr = identity ? cm_copy_rows(in_data, inputW, output, w, h, 16)
			                : cm_render_bgra_32f(in_data, inputW, output, w, h, &info);
		} else {
			perr = identity ? cm_copy_rows(in_data, inputW, output, w, h, 4)
			                : cm_render_bgra_8u(in_data, inputW, output, w, h, &info);
		}
		cm_trace(perr == PF_Err_NONE ? "  -> done" : "  -> aborted (host superseded)");
		// Premiere logs ANY non-zero return from a Pr-format render as "a low-level
		// exception". A host-superseded render isn't an error — we already stopped
		// early to save CPU; just report success. The discarded frame is moot.
		return PF_Err_NONE;   // NEVER hand a Premiere world to the AE PF_ITERATE path
	}

	// ---------------- After Effects (and fallback): 8-bit ARGB --------------
	// Only take the AE path for a genuine host: appl_id must be a printable 4-char
	// code ('FXTC' for After Effects, etc.). Premiere sometimes issues anomalous
	// render calls with an uninitialised in_data (garbage appl_id / pica pointer);
	// running PF_ITERATE/PF_COPY on those faults. For anything that isn't a real
	// host, do nothing and report success — the frame is transient.
	{
		unsigned a = (unsigned)in_data->appl_id;
		int printable = 1;
		for (int k = 0; k < 4; k++) { unsigned c = (a >> (k * 8)) & 0xFF; if (c < 0x20 || c > 0x7E) printable = 0; }
		if (!printable) {
			cm_trace("anomalous in_data (appl=0x%x) -> no-op", a);
			return PF_Err_NONE;
		}
	}
	cm_trace("AE render appl=0x%x slot=%ld size=%d identity=%d in=%ldx%ld",
	         (unsigned)in_data->appl_id, (long)slot, lut_size, identity,
	         (long)inputW->width, (long)inputW->height);
	if (identity) {
		return PF_COPY(inputW, output, NULL, NULL);
	}

	if (in_data->extent_hint.left != output->extent_hint.left ||
		in_data->extent_hint.top != output->extent_hint.top ||
		in_data->extent_hint.right != output->extent_hint.right ||
		in_data->extent_hint.bottom != output->extent_hint.bottom) {
		ERR(PF_FILL(NULL, &output->extent_hint, output));
	}

	A_long hh = in_data->extent_hint.bottom - in_data->extent_hint.top;
	ERR(PF_ITERATE(0, hh, inputW, &in_data->extent_hint, (void *)&info, cm_pixel, output));
	cm_trace("AE PF_ITERATE done err=%d", (int)err);
	return err;
}

extern "C" DllExport
PF_Err PluginDataEntryFunction2(
	PF_PluginDataPtr inPtr, PF_PluginDataCB2 inPluginDataCallBackPtr,
	SPBasicSuite *inSPBasicSuitePtr, const char *inHostName, const char *inHostVersion) {
	PF_Err result = PF_Err_INVALID_CALLBACK;
	result = PF_REGISTER_EFFECT_EXT2(
		inPtr, inPluginDataCallBackPtr,
		"colourMatik",              // Name (shows in Effects panel)
		CM_MATCH_NAME,              // Match Name (unique id; the AE build has its own)
		"colourMatik",              // Category
		AE_RESERVED_INFO,
		"EffectMain",
		"https://catheadai.com");
	return result;
}

PF_Err EffectMain(PF_Cmd cmd, PF_InData *in_data, PF_OutData *out_data, PF_ParamDef *params[], PF_LayerDef *output, void *extra) {
	PF_Err err = PF_Err_NONE;
	switch (cmd) {
	case PF_Cmd_ABOUT:            err = About(in_data, out_data, params, output); break;
	case PF_Cmd_GLOBAL_SETUP:     err = GlobalSetup(in_data, out_data, params, output); break;
	case PF_Cmd_PARAMS_SETUP:     err = ParamsSetup(in_data, out_data, params, output); break;
	case PF_Cmd_SEQUENCE_SETUP:   err = SequenceSetup(in_data, out_data, params, output); break;
	case PF_Cmd_SEQUENCE_SETDOWN: err = SequenceSetdown(in_data, out_data, params, output); break;
	case PF_Cmd_SEQUENCE_RESETUP: err = SequenceResetup(in_data, out_data, params, output); break;
	case PF_Cmd_RENDER:           err = Render(in_data, out_data, params, output); break;
#ifdef CM_AE_BUILD
	case PF_Cmd_SMART_PRE_RENDER: err = PreRender(in_data, out_data, (PF_PreRenderExtra *)extra); break;
	case PF_Cmd_SMART_RENDER:     err = SmartRender(in_data, out_data, (PF_SmartRenderExtra *)extra); break;
#endif
	}
	return err;
}

#ifdef AE_OS_WIN
BOOL WINAPI DllMain(HINSTANCE hDLL, DWORD dwReason, LPVOID lpReserved) { return TRUE; }
#endif
