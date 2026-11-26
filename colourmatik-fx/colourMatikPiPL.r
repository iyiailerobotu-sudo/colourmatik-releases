#include "AEConfig.h"
#include "AE_EffectVers.h"

#ifndef AE_OS_WIN
	#include "AE_General.r"
#endif

resource 'PiPL' (16000) {
	{	/* array properties: 12 elements */
		/* [1] */
		Kind {
			AEEffect
		},
		/* [2] */
		Name {
			"colourMatik"
		},
		/* [3] */
		Category {
			"colourMatik"
		},
#ifdef AE_OS_WIN
    #if defined(AE_PROC_INTELx64)
		CodeWin64X86 {"EffectMain"},
    #elif defined(AE_PROC_ARM64)
		CodeWinARM64 {"EffectMain"},
    #endif
#elif defined(AE_OS_MAC)
		CodeMacIntel64 {"EffectMain"},
		CodeMacARM64 {"EffectMain"},
#endif
		/* [6] */
		AE_PiPL_Version {
			2,
			0
		},
		/* [7] */
		AE_Effect_Spec_Version {
			PF_PLUG_IN_VERSION,
			PF_PLUG_IN_SUBVERS
		},
		/* [8] */
		AE_Effect_Version {
#ifdef CM_AE_BUILD
			589825	/* 1.2 */
#else
			557057	/* 1.1 */
#endif
		},
		/* [9] */
		AE_Effect_Info_Flags {
			0
		},
		/* [10] */
		AE_Effect_Global_OutFlags {
#ifdef CM_AE_BUILD
			33555520	/* USE_OUTPUT_EXTENT | PIX_INDEPENDENT | DEEP_COLOR_AWARE (1<<25) */
#else
			1088		/* USE_OUTPUT_EXTENT | PIX_INDEPENDENT */
#endif
		},
		/* [11] */
		AE_Effect_Match_Name {
#ifdef CM_AE_BUILD
			"catheadaiAEcolorMatik"
#else
			"catheadai colourMatik"
#endif
		},
		/* [12] */
		AE_Reserved_Info {
			0
		},
		/* [13] */
		AE_Effect_Global_OutFlags_2 {
#ifdef CM_AE_BUILD
			134222848    /* THREADED_RENDERING (1<<27) | SUPPORTS_SMART_RENDER (1<<10) | FLOAT_COLOR_AWARE (1<<12) */
#else
			134217728    /* PF_OutFlag2_SUPPORTS_THREADED_RENDERING (1<<27) */
#endif
		},
		/* [14] */
		AE_Effect_Support_URL {
			"https://catheadai.com"
		}
	}
};
