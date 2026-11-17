// SPDX-FileCopyrightText: Copyright 2026 Eden Emulator Project
// SPDX-License-Identifier: GPL-3.0-or-later

#pragma once

#include <atomic>
#include <mutex>

#include "common/common_types.h"
#include "common/settings.h"
#include "video_core/engines/maxwell_3d.h"

// The Legend of Zelda: Ocarina of Time's anti-aliasing. After tonemapping, the game runs a
// single-frame tensor network on the tonemapped image and an output pass that sharpens the result
// hard. With the "Ocarina of Time anti-aliasing" setting at No sharpening, the network is skipped
// and its output pass is replaced by a copy of the tonemapped image
// (docs/nx2/oot-network-bypass.md).
//
// Shader hashes, native program keys and constant buffer offsets are those of the game's main
// with build id E0F3CCFAF50065AE4EA01EB5AEB13B1A45DA3348; a game update needs them re-checked.
namespace VideoCommon::OotNetwork {

constexpr u64 ProgramId = 0x0400486024DEC000ULL;

// Native program keys of the network's compute passes.
constexpr u64 PrepassProgramKey = 0x8475d671e6967428ULL;
// Match the native key, not this pass's lowered shader hash (d9ee412f00ce399a).
constexpr u64 UnusedDispatchProgramKey = 0x9387db798781f081ULL;
constexpr u64 NetworkProgramKey = 0x42593194bb0b2b32ULL;
constexpr u64 OutputProgramKey = 0xda6df3e54c716df2ULL;

/// Constant buffer 0 offset of the output pass's texture handle for the image it samples. On the
/// update this is the HDR scene the tonemap pass reads, not the tonemapped image.
constexpr u32 InputImageHandleOffset = 0xc0;

/// Constant buffer 0 offset of the output pass's texture handle for the B10G11R11 image it writes
/// (the scene image the rest of the frame uses).
constexpr u32 OutputImageHandleOffset = 0xd0;

/// Owned by the rasterizer: the tonemap pass draws on the 3D engine, and the network's passes do
/// not all run on the same compute engine.
struct BypassState {
    std::mutex mutex;
    /// Render target 0 of the latest draw IsTonemapCandidate accepted since the last output pass,
    /// under mutex. Valid while latest_address is not 0.
    Tegra::Engines::Maxwell3D::Regs::RenderTargetConfig latest_draw{};
    std::atomic<u64> latest_address{};
    /// The input's size and the output's address at the latest output pass, 0 until one is seen.
    std::atomic<u32> input_width{};
    std::atomic<u32> input_height{};
    std::atomic<u64> output_address{};
    std::atomic<bool> output_replaced{}; ///< the latest output pass was replaced
    /// The prepass was skipped and no output pass has been replaced since.
    std::atomic<bool> output_pending{};
    /// The first native program dispatched after the network, 0 until one is seen.
    std::atomic<u64> after_network{};
};

/// Whether a B10G11R11 draw can hold the image the output pass shows: it has the size of the
/// output pass's input and is not its output. The latest such draw before the output pass is the
/// tonemap, whichever shader draws it, because it comes after the scene draws into the input.
constexpr bool IsTonemapCandidate(u64 address, u32 width, u32 height, u32 input_width,
                                  u32 input_height, u64 output_address) {
    return address != 0 && address != output_address && input_width != 0 && input_height != 0 &&
           width == input_width && height == input_height;
}

/// This program feeds only the bypassed network. Do not skip it outside its prepass window.
inline bool ShouldSkipUnusedDispatch(u64 native_key, bool output_replaced, bool output_pending) {
    return native_key == UnusedDispatchProgramKey && output_replaced && output_pending;
}

inline bool BypassEnabled(u64 program_id) {
    return program_id == ProgramId &&
           Settings::values.oot_anti_aliasing.GetValue() == Settings::OotAntiAliasing::NoSharpening;
}

} // namespace VideoCommon::OotNetwork
