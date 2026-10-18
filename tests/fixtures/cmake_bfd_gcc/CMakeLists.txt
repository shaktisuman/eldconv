cmake_minimum_required(VERSION 3.16)
project(hello C)
add_executable(hello main.c)
target_link_options(hello PRIVATE -Wl,--gc-sections -Wl,--print-memory-usage -Wl,--no-warn-rwx-segments)
