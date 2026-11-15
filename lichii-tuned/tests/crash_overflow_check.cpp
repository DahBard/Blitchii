// SPDX-License-Identifier: GPL-3.0-or-later

// Standalone check for src/common/windows/crash_handler.cpp, built and run by
// lichii-tuned-tests.yml on the patched tree: installs the handler, then (with an argument)
// recurses until the stack overflows. "overflow" recurses on a std::thread worker, "guarded" on
// one that keeps a stack guarantee as the shader job workers do, "main" on the main thread,
// "chkstk" through frames large enough to call ___chkstk_ms. Each should leave a "Host crash"
// line on stderr and eden_crash.dmp in the current directory.
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <thread>

#include <windows.h>

#include "common/logging.h"
#include "common/windows/crash_handler.h"

namespace Common::Log {
// The crash handler's only dependency on the logging backend.
void FmtCrashMessageImpl(const char* filename, unsigned int line_num, const char* function,
                         fmt::string_view format, const fmt::format_args& args) noexcept {
    char buffer[2048];
    const auto result = fmt::vformat_to_n(buffer, sizeof(buffer) - 1, format, args);
    buffer[(std::min)(result.size, sizeof(buffer) - 1)] = '\0';
    std::fprintf(stderr, "%s:%u:%s: %s\n", filename, line_num, function, buffer);
    std::fflush(stderr);
}
} // namespace Common::Log

namespace {

__attribute__((noinline)) int Recurse(volatile char* previous, int depth) {
    volatile char frame[256];
    frame[0] = static_cast<char>(depth);
    frame[255] = previous ? previous[0] : 0;
    return Recurse(frame, depth + 1) + frame[1];
}

__attribute__((noinline)) int RecurseLarge(volatile char* previous, int depth) {
    volatile char frame[8192];
    frame[0] = static_cast<char>(depth);
    frame[8191] = previous ? previous[0] : 0;
    return RecurseLarge(frame, depth + 1) + frame[1];
}

} // namespace

int main(int argc, char** argv) {
    char directory[MAX_PATH];
    GetCurrentDirectoryA(sizeof(directory), directory);
    Common::Windows::InstallCrashHandler(directory);
    if (argc < 2) {
        std::puts("crash handler installed; pass overflow, guarded, main or chkstk");
        return 0;
    }
    int result = 0;
    if (std::strcmp(argv[1], "main") == 0) {
        result = Recurse(nullptr, 0);
    } else if (std::strcmp(argv[1], "guarded") == 0) {
        std::thread worker([&result] {
            Common::Windows::GuaranteeStackForCrashReport();
            result = Recurse(nullptr, 0);
        });
        worker.join();
    } else if (std::strcmp(argv[1], "chkstk") == 0) {
        std::thread worker([&result] { result = RecurseLarge(nullptr, 0); });
        worker.join();
    } else {
        std::thread worker([&result] { result = Recurse(nullptr, 0); });
        worker.join();
    }
    return result;
}
