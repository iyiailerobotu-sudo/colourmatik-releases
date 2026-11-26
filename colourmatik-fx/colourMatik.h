/*
	colourMatik — native After Effects / Premiere Pro effect.
	Applies a colourMatik 3D LUT (written by the engine to a per-instance "slot"
	.cube file) with a built-in Intensity slider. The heavy colour-matching is
	done by the local engine; this effect just applies its result as a real,
	named "colourMatik" effect the panel can add + configure.

	Rendering: in Premiere Pro the effect declares the 32-bit float BGRA pixel
	format (with an 8-bit BGRA fallback), so it applies the engine's 65^3 LUT at
	full float precision — zero quantisation, no banding. In After Effects
	(CM_AE_BUILD) it renders at the project's depth: 8, 16 or 32 bits.
*/
#ifndef COLOURMATIK_H
#define COLOURMATIK_H

#ifdef _WIN32
#define _CRT_SECURE_NO_WARNINGS 1   // getenv/fopen/sscanf are fine here; sample builds with /WX
#endif

#include "AEConfig.h"

#ifdef AE_OS_WIN
#include <Windows.h>
#endif

#include "entry.h"
#include "AE_Effect.h"
#include "A.h"
#include "AE_EffectCB.h"
#include "AE_EffectCBSuites.h"
#include "AE_Macros.h"
#include "Param_Utils.h"
#include "SPTypes.h"          // SPAPI, needed by the Premiere suites below
#include "SPBasic.h"          // full SPBasicSuite (AcquireSuite/ReleaseSuite)
#include "PrSDKAESupport.h"

#define MAJOR_VERSION   1
#ifdef CM_AE_BUILD
#define MINOR_VERSION   2   // After Effects build: 8/16/32-bit SmartFX, linear working space
#else
#define MINOR_VERSION   1   // Premiere build: unchanged since 1.1
#endif
#define BUG_VERSION     0
#define STAGE_VERSION   PF_Stage_DEVELOP
#define BUILD_VERSION   1

#define NAME        "colourMatik"
#define DESCRIPTION "One-click colour matching. by Sevki Bugra Ozbek - catheadai.com"

#ifndef kAppID_Premiere
#define kAppID_Premiere 'PrMr'
#endif

enum {
	CM_INPUT = 0,   // default input layer
	CM_INTENSITY,   // 0..200 %, 100 = full match
	CM_SLOT,        // which slot_<N>.cube to load (set by the panel)
#ifdef CM_AE_BUILD
	CM_LINEAR,      // the project's working space is linearised (set by the panel)
#endif
	CM_NUM_PARAMS
};

enum { CM_INTENSITY_DISK_ID = 1, CM_SLOT_DISK_ID = 2, CM_LINEAR_DISK_ID = 3 };

// Two builds from this source. The Premiere one (MediaCore) keeps its proven
// flags and pixel-format-suite path. The After Effects one (CM_AE_BUILD) is a
// SmartFX effect that works at the project's own depth - 8, 16 or 32 bits per
// channel - instead of making AE squeeze a deep project down to 8 bits for it,
// and knows a linearised working space. The PiPL (colourMatikPiPL.r) follows
// the same switch: AE checks its flags and version against GlobalSetup's.
#ifdef CM_AE_BUILD
#define CM_MATCH_NAME "catheadaiAEcolorMatik"
#else
#define CM_MATCH_NAME "catheadai colourMatik"
#endif

#define CM_INTENSITY_MIN     0
#define CM_INTENSITY_MAX     200
#define CM_INTENSITY_DFLT    100
#define CM_SLOT_MIN          0
#define CM_SLOT_MAX          99999
#define CM_SLOT_DFLT         0

#define CM_LUT_MAX   65      // largest .cube edge we accept (engine writes 65^3)
#define CM_LUT_N3MAX (CM_LUT_MAX * CM_LUT_MAX * CM_LUT_MAX)

// The LUT is NOT stored in sequence data. After Effects Multi-Frame Rendering
// nulls in_data->sequence_data on worker threads, so the effect keeps the LUT in
// a per-thread, lock-free cache instead (see cm_get_lut in colourMatik.cpp). The
// slot number persists in the CM_SLOT param; the LUT reloads from slot_<N>.cube.

extern "C" {
	DllExport PF_Err EffectMain(
		PF_Cmd cmd, PF_InData *in_data, PF_OutData *out_data,
		PF_ParamDef *params[], PF_LayerDef *output, void *extra);
}

#endif // COLOURMATIK_H
