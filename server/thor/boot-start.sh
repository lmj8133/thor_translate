#!/data/data/com.termux/files/usr/bin/sh
# Termux:Boot hook - starts the proxy after device boot.
#
# Runs run-proxy.sh in the FOREGROUND on purpose: the live task keeps
# TermuxService (and its notification) running, which keeps this uid off
# Android's empty-process reaper. The 2026-08-03 outage was ActivityManager
# reaping the whole uid ("Killing com.termux (adj 985): empty") after the
# old backgrounding version exited and left the service with nothing alive.
#
# Still NO wake lock: the CPU sleeps with the screen (translation only
# happens screen-on), so idle battery cost stays zero.
exec "$HOME/thor-proxy/server/thor/run-proxy.sh" >> "$HOME/thor-proxy/proxy.log" 2>&1
