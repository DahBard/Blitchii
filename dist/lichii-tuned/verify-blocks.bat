@echo off
rem Checks the 3D register blocks (r62, upstream #1585: the GPU thread sends runs of 3D state
rem writes to its back thread as one entry) against the writes one by one, with the perf-trace
rem logging. Slower: not for frame rates. Every 10 s, the 'Register block verify' line's blocks
rem that differ must stay 0. verify.bat can't check them: its EDEN_NX2_PERF_VERIFY turns the
rem blocks off.
set EDEN_NX2_REG_BLOCK_VERIFY=1&& set EDEN_NX2_GPU_PROFILE=1&& set EDEN_DKB_FRAME_TRACE=1
start "" "%~dp0lichii.exe" %*
