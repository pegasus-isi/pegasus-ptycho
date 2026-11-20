#!/bin/bash

# this file is use sources when the job starts up and
# used to provide a common environment for jobs across
# resources

export PATH=/usr/bin:/bin
export USER=`whoami`

source /etc/profile.d/modules.sh

hostname=`hostname -f`
stampede_pattern='^[A-Za-z0-9-]+.stampede2.tacc.utexas.edu$'
expanse_pattern='^[A-Za-z0-9-]+.expanse.sdsc.edu$'
carc_and_laguna_pattern='^[A-Za-z0-9-]+.[A-Za-z0-9-]+.usc.edu$' 
if [[ $hostname =~ $stampede_pattern ]]; then
    echo "Sourcing job environment for Stampede2"
    # initialize modules
    . /etc/profile.d/z01_lmod.sh
    module load tacc-singularity
elif [[ $hostname =~ $expanse_pattern ]]; then
    echo "Sourcing job environment for Expanse"
    source /etc/profile.d/modules.sh
    module load singularitypro
elif [[ $hostname =~ $carc_and_laguna_pattern ]]; then
    echo "Sourcing job environment for USC CARC and USC Laguna"
    source /etc/profile.d/modules.sh
    module load apptainer

    # for ptycho code
    module load legacy/CentOS7  gcc/12.3.0
    module load cuda/12.2.1
    module load matlab

    # force our matlab wrapper to execute instead of what module load matlab does
    export PATH=/home1/vahi/pegasus-ptycho/bin:$PATH
    #apptainer exec --bind /apps --bind /spack --env PREPEND_PATH=/apps/generic/matlab/2025b/bin:/spack/compilers/gcc/12.3.0/bin/ /apps/containers/ood/desktop/rocky8-xfce-desktop-ood4.sif /bin/bash
else
    echo "No specific job environment sourced"
fi



