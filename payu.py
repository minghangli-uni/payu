#!/usr/bin/env python
# coding: utf-8
"""
Payu: A generic driver for numerical models on the NCI computing cluster (vayu)
-------------------------------------------------------------------------------
Contact: Marshall Ward <marshall.ward@anu.edu.au>
-------------------------------------------------------------------------------
Distributed as part of Payu, Copyright 2011 Marshall Ward
Licensed under the Apache License, Version 2.0
http://www.apache.org/licenses/LICENSE-2.0
"""

import os
import sys
import shutil as sh
import subprocess as sp
import shlex
import grp
import getpass
import errno
import yaml

# Environment module support on vayu
execfile('/opt/Modules/default/init/python')
module_path = '/projects/v45/modules'
default_modules = ['python/2.7.3', 'python/2.7.3-matplotlib', 'payu']

# Default payu parameters
counter_env = 'count'
max_counter_env = 'max'
default_archive_url = 'dc.nci.org.au'
default_model_script = 'model.py'
default_collate_script = 'collate.py'
default_config_fname = 'config.yaml'

#==============================================================================
class Experiment(object):

    #---
    def __init__(self, **kwargs):
        self.model_name = None
        self.modules = None
        self.config_files = None

        self.model_script = kwargs.pop('model_script', default_model_script)
        self.collation_script = kwargs.pop('collate_script',
                                           default_collate_script)
        self.set_counters()


    #---
    def set_counters(self):
        self.counter = int(os.environ.get(counter_env, 0))
        self.max_counter = int(os.environ.get(max_counter_env, self.counter))
        assert 0 <= self.max_counter
        assert 0 <= self.counter <= self.max_counter


    #---
    def load_modules(self):
        module('purge')

        # Ensure python and payu remain loaded
        module('use', module_path)
        for mod in default_modules:
            module('load', mod)

        # Now load model-dependent modules
        for mod in self.modules:
            module('load', mod)


    #---
    def path_names(self, **kwargs):
        assert self.model_name

        config_fname = kwargs.pop('config', default_config_fname)

        try:
            with open(config_fname, 'r') as config_file:
                config = yaml.load(config_file)
        except IOError, ec:
            if ec.errno != errno.ENOENT:
                raise
            else:
                config = {}

        # Override any keyword arguments
        config.update(kwargs)

        # Experiment name (used for directories)
        default_name = os.path.basename(os.getcwd())
        self.name = config.pop('jobname', default_name)

        # Configuration path (input, config)
        default_config_path = os.getcwd()
        self.config_path = config.pop('config', default_config_path)

        # User name
        default_user = getpass.getuser()
        self.user_name = config.pop('user', default_user)

        # Top level output path ("/short path")
        default_short_path = os.path.join('/short', os.environ.get('PROJECT'))
        self.short_path = config.pop('shortpath', default_short_path)

        # Output path
        default_lab_path = os.path.join(self.short_path,
                                        self.user_name, self.model_name)
        self.lab_path = config.pop('output', default_lab_path)

        # Experiment subdirectories
        self.archive_path = os.path.join(self.lab_path, 'archive', self.name)
        self.bin_path = os.path.join(self.lab_path, 'bin')
        self.work_path = os.path.join(self.lab_path, 'work', self.name)

        # Symbolic path to work
        self.work_sym_path = os.path.join(self.config_path, 'work')
        self.archive_sym_path = os.path.join(self.config_path, 'archive')

        # Set group identifier for output
        self.archive_group = config.pop('archive_group', None)

        # Executable path
        exec_name = config.pop('exe', self.default_exec)
        self.exec_path = os.path.join(self.bin_path, exec_name)

        # Stream output filenames
        self.stdout_fname = self.model_name + '.out'
        self.stderr_fname = self.model_name + '.err'

        # External input path
        input_dir = config.pop('input', None)
        if input_dir:
            # Test for absolute path
            if os.path.exists(input_dir):
                self.input_path = input_dir
            else:
                # Test for path relative to /${lab_path}/input
                rel_path = os.path.join(self.lab_path, 'input', input_dir)
                if os.path.exists(rel_path):
                    self.input_path = rel_path
                else:
                    sys.exit('Input data not found; aborting.')
        else:
            self.input_path = None

        # Local archive paths
        # TODO: Rename this to self.output_path
        output_dir = 'output%03i' % (self.counter,)
        self.output_path = os.path.join(self.archive_path, output_dir)

        # TODO: check case counter == 0
        prior_output_dir = 'output%03i' % (self.counter - 1,)
        prior_output_path = os.path.join(self.archive_path, prior_output_dir)
        if os.path.exists(prior_output_path):
            self.prior_output_path = prior_output_path
        else:
            self.prior_output_path = None

        # Local restart paths
        res_dir = 'restart%03i' % (self.counter,)
        self.res_path = os.path.join(self.archive_path, res_dir)

        prior_res_dir = 'restart%03i' % (self.counter - 1,)
        prior_res_path = os.path.join(self.archive_path, prior_res_dir)
        if os.path.exists(prior_res_path):
            self.prior_res_path = prior_res_path
        else:
            self.prior_res_path = None
            if self.counter > 0:
                # TODO: This warning should be replaced with an abort in setup
                print 'Warning: no restart files found.'


    #---
    def setup(self, do_stripe=False):
        # Confirm that no output path already exists
        if os.path.exists(self.output_path):
            sys.exit('Archived path already exists; aborting.')

        mkdir_p(self.work_path)

        # Stripe directory in Lustre
        if do_stripe:
            cmd = 'lfs setstripe -c 8 -s 8m {0}'.format(self.work_path).split()
            rc = sp.Popen(cmd).wait()
            assert rc == 0

        make_symlink(self.work_path, self.work_sym_path)

        for f in self.config_files:
            f_path = os.path.join(self.config_path, f)
            sh.copy(f_path, self.work_path)


    #---
    def run(self, *mpi_flags):
        f_out = open(self.stdout_fname, 'w')
        f_err = open(self.stderr_fname, 'w')

        mpi_cmd = 'mpirun'  # OpenMPI execute

        # Convert flags to list of arguments
        flags = [c for flag in mpi_flags for c in flag.split(' ')]
        cmd = [mpi_cmd] + flags + [self.exec_path]

        rc = sp.Popen(cmd, stdout=f_out, stderr=f_err).wait()
        f_out.close()
        f_err.close()

        # Remove any empty output files (e.g. logs)
        for fname in os.listdir(self.work_path):
            fpath = os.path.join(self.work_path, fname)
            if os.path.getsize(fpath) == 0:
                os.remove(fpath)

        # TODO: Need a model-specific cleanup method call here
        if rc != 0:
            sys.exit('Error {0}; aborting.'.format(rc))

        # Move logs to archive (or delete if empty)
        for f in (self.stdout_fname, self.stderr_fname):
            if os.path.getsize(f) == 0:
                os.remove(f)
            else:
                sh.move(f, self.work_path)


    #---
    def archive(self, collate=True):
        mkdir_p(self.archive_path)

        make_symlink(self.archive_path, self.archive_sym_path)

        # Remove work symlink
        if os.path.islink(self.work_sym_path):
            os.remove(self.work_sym_path)

        # Double-check that the run path does not exist
        if os.path.exists(self.output_path):
            sys.exit('Archived path already exists; aborting.')

        cmd = 'mv {src} {dst}'.format(src=self.work_path, dst=self.output_path)
        rc = sp.Popen(cmd.split()).wait()
        assert rc == 0

        if self.archive_group:
            self.regroup()

        if collate:
            cmd = ['qsub', self.collation_script, '-v', '%s=%i'
                    % (counter_env, self.counter)]
            rc = sp.Popen(cmd).wait()


    #---
    def remote_archive(self, config_name, archive_url=None,
                       max_rsync_attempts=1, rsync_protocol=None):

        if not archive_url:
            archive_url = default_archive_url

        archive_address = '{usr}@{url}'.format(usr=getpass.getuser(),
                                               url=archive_url)

        ssh_key_path = os.path.join(os.getenv('HOME'), '.ssh',
                                    'id_rsa_file_transfer')

        # Top-level path is implicitly set by the SSH key
        # (Usually /projects/[group])

        # Remote mkdir is currently not possible, so any new subdirectories
        # must be created before auto-archival

        remote_path = os.path.join(self.model_name, config_name, self.name)
        remote_url = '{addr}:{path}'.format(addr=archive_address,
                                            path=remote_path)

        # Rsync ouput and restart files
        rsync_cmd = 'rsync -a --safe-links -e "ssh -i {key}" '.format(
                        key=ssh_key_path)

        if rsync_protocol:
            rsync_cmd += '--protocol={p} '.format(p=rsync_protocol)

        run_cmd = rsync_cmd + '{src} {dst}'.format(src=self.output_path,
                                                   dst=remote_url)
        rsync_calls = [run_cmd]

        if (self.counter % 5) == 0 and os.path.isdir(self.res_path):
            # Tar restart files before rsyncing
            res_tar_path = self.res_path + '.tar.gz'

            cmd = 'tar -C {path} -czf {fpath} {res}'.format(
                        path=self.archive_path,
                        fpath=res_tar_path,
                        res=os.path.basename(self.res_path)
                        ).split()
            rc = sp.Popen(cmd).wait()

            restart_cmd = rsync_cmd + '{src} {dst}'.format(src=res_tar_path,
                                                           dst=remote_url)
            rsync_calls.append(restart_cmd)
        else:
            res_tar_path = None

        if self.input_path and os.path.isdir(self.input_path):
            # Using explicit path separators to rename the input directory
            input_cmd = rsync_cmd + '{src} {dst}'.format(
                            src=self.input_path + os.sep,
                            dst=os.path.join(remote_url, 'input') + os.sep)
            rsync_calls.append(input_cmd)

        for cmd in rsync_calls:
            cmd = shlex.split(cmd)

            for rsync_attempt in range(max_rsync_attempts):
                rc = sp.Popen(cmd).wait()
                if rc == 0:
                    break
                else:
                    print 'rsync failed, reattempting'
            assert rc == 0

        # TODO: Temporary; this should be integrated with the rsync call
        if res_tar_path and os.path.exists(res_tar_path):
            os.remove(res_tar_path)


    #---
    def regroup(self):
        uid = os.getuid()
        gid = grp.getgrnam(self.archive_group).gr_gid

        os.lchown(self.archive_path, uid, gid)
        for root, dirs, files in os.walk(self.archive_path):
            for d in dirs:
                os.lchown(os.path.join(root, d), uid, gid)
            for f in files:
                os.lchown(os.path.join(root, f), uid, gid)


    #---
    def resubmit(self):
        next_run = self.counter + 1
        n_runs = self.max_counter - next_run + 1
        cmd = 'payu run -i {0} -n {1}'.format(next_run, n_runs)

        cmd = shlex.split(cmd)
        sp.Popen(cmd).wait()


    #---
    def sweep(self, hard_sweep=False):
        #f = open(self.model_script, 'r')
        #for line in f:
        #    if line.startswith('#PBS -N '):
        #        expt_name = line.strip().replace('#PBS -N ', '')
        #f.close()

        f = open(self.collation_script, 'r')
        for line in f:
            if line.startswith('#PBS -N '):
                coll_name = line.strip().replace('#PBS -N ', '')
        f.close()

        if hard_sweep:
            if os.path.isdir(self.archive_path):
                print 'Removing archive path %s' % self.archive_path
                #sh.rmtree(self.archive_path)
                cmd = 'rm -rf {0}'.format(self.archive_path).split()
                rc = sp.Popen(cmd).wait()
                assert rc == 0

            if os.path.islink(self.archive_sym_path):
                print 'Removing symlink %s' % self.archive_sym_path
                os.remove(self.archive_sym_path)

        if os.path.isdir(self.work_path):
            print 'Removing work path %s' % self.work_path
            #sh.rmtree(self.work_path)
            cmd = 'rm -rf {0}'.format(self.work_path).split()
            rc = sp.Popen(cmd).wait()
            assert rc == 0

        if os.path.islink(self.work_sym_path):
            print 'Removing symlink %s' % self.work_sym_path
            os.remove(self.work_sym_path)

        # TODO: model outstreams and pbs logs need to be handled separately
        logs = [f for f in os.listdir(os.curdir) if os.path.isfile(f) and
                (f == self.stdout_fname or
                 f == self.stderr_fname or
                 f.startswith(self.name + '.o') or
                 f.startswith(self.name + '.e') or
                 f.startswith(coll_name + '.o') or
                 f.startswith(coll_name + '.e')
                 )
                ]

        pbs_log_path = os.path.join(os.curdir, 'pbs_logs')
        mkdir_p(pbs_log_path)

        for f in logs:
            print 'Moving log {fname}'.format(fname=f)
            os.rename(f, os.path.join(pbs_log_path, f))


#==============================================================================
def mkdir_p(path):
    try:
        os.makedirs(path)
    except OSError, ec:
        if ec.errno != errno.EEXIST:
            raise


#---
def make_symlink(path, link):
    try:
        os.symlink(path, link)
    except OSError, ec:
        if ec.errno != errno.EEXIST:
            raise
        elif not os.path.islink(link):
            # Warn the user, but do not interrput the job
            print("Warning: Cannot create symbolic link to {p}; a file named "
                  "{f} already exists.".format(p=path, f=link))
        else:
            # Overwrite any existing symbolic link
            if os.path.realpath(link) != path:
                os.remove(link)
                os.symlink(path, link)
