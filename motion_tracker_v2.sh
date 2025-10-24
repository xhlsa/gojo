#!/bin/bash
# Motion Tracker V2 launcher - runs the tracker from the scripts folder

cd "$(dirname "$0")" || exit
python motion_tracker_v2/motion_tracker_v2.py "$@"
