#!/bin/bash

# this file is use sources when the job starts up and
# used to provide a common environment for jobs across
# resources

export PATH=/usr/bin:/bin
export USER=`whoami`

source /etc/profile.d/modules.sh
module load matlab
