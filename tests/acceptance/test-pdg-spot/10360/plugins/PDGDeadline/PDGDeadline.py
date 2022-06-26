#
# Copyright (c) <2020> Side Effects Software Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# NAME:         PDGDeadline.py ( Python )
#
# COMMENTS:     Custom Deadline plugin for Houdini TOP Deadline scheduler.
#               Supports PDG work items as tasks inside a single PDG job.
#               Task data is provided via a task file that the caller has written out.
#               The current frame corresponds to the task file's identity.
#               Evaluates PDG variables locally and sets process environment.

import os
import sys
import json
import re
import traceback

# pylint: disable=import-error,no-member,undefined-variable
from System import DateTime, TimeSpan
from System.IO import *
from System.Text.RegularExpressions import *

from Deadline.Scripting import *
from Deadline.Plugins import *

from FranticX.Processes import *

# MQ server connection expression
conn_reg = re.compile(r'PDG_MQ (\S+) (\d+) (\d+)')
conn_reg_pdgutilset = re.compile(r'PDG_MQ (\S+) (\d+) (\d+) (\d+)')

def GetDeadlinePlugin():
    return PDGDeadlinePlugin()

def CleanupDeadlinePlugin(deadlinePlugin):
    deadlinePlugin.Cleanup()

class PDGDeadlinePlugin(DeadlinePlugin):
    def __init__(self):
        self.InitializeProcessCallback += self.InitializeProcess
        self.RenderTasksCallback += self.RenderTasks

        # Handler shutting down Monitor program in case its still running
        self.EndJobCallback += self.EndJob

        # Callback when the Monitor program exits
        self.MonitoredProgramExitCallback += self.MonitoredManagedProgramExit

        # The name of the monitor program to run. If this is set
        # then the monitor program is most likely running.
        self.monitorProgramName = None

        # Timeout for waiting on task file (milliseconds)
        self.taskFileTimeout = 1E5

        # Timeout for waiting on MQ connection file (milliseconds)
        self.mqFileTimeout = 1E5

        # Timeout for waiting on shutting down monitor program (milliseconds)
        self.monitorShutDownTimeout = 3E5

        # Force kill Monitor program in case of error
        self.forceKillMonitor = False

        #Managed process handle.
        self.wProcess = None

    def Cleanup(self):
        for stdoutHandler in self.StdoutHandlers:
            del stdoutHandler.HandleCallback

        del self.InitializeProcessCallback
        del self.RenderTasksCallback
        del self.EndJobCallback
        del self.MonitoredProgramExitCallback

        #Clean up managed process:
        if self.wProcess:
            self.wProcess.Cleanup()
            del self.wProcess

    def InitializeProcess(self):
        self.LogInfo('*********** PDGDeadline InitializeProcess')
        self.SingleFramesOnly = False
        self.PluginType = PluginType.Advanced
        self.StdoutHandling = True

        self.monitorProgramName = None

    def RenderTasks(self):
        """
        Executes a single frame as a task by looking up the task info file
        and parsing the command, expands local environment variables,
        and executes as a child process.
        """
        try:
            startFrame = self.GetStartFrame()
            self.LogInfo('StartFrame: {}'.format(startFrame))
            #self.LogInfo ('ThreadNumber: {}'.format(self.GetThreadNumber()))
            #self.LogInfo ('Current Task: {}'.format(self.GetCurrentTaskId()))

            startupDir = self.GetStartupDirectory()

            # Get current plaform
            os_type = 'linux'
            path_combine = ':'
            if sys.platform == 'linux' or sys.platform == 'linux2':
                os_type = 'linux'
            elif sys.platform == 'win32':
                os_type = 'windows'
                path_combine = ';'
            elif sys.platform == 'darwin':
                os_type = 'macos'

            job = self.GetJob()
            json_obj = None

            pdgJobType = job.GetJobExtraInfoKeyValue('PDGJobType')
            if pdgJobType == 'MQJob':
                executable = job.GetJobExtraInfoKeyValue('MQExecutable')
                arguments = job.GetJobExtraInfoKeyValue('MQArgs')
                if not executable or not arguments:
                    self.FailRender('No MQExecutable or MQArgs in job key-values.')

                executable = RepositoryUtils.CheckPathMapping(executable)
                arguments = RepositoryUtils.CheckPathMapping(arguments)
            else:
                # The PDG job directory will contain the task file
                jobDir = self.GetPluginInfoEntryWithDefault('PDGJobDirectory', '')
                if not jobDir:
                    self.FailRender('PDGJobDirectory is not specified. Unable to get task file.')

                jobDir = RepositoryUtils.CheckPathMapping(jobDir)

                # Task job
                taskFilePath = os.path.join(jobDir, 'task_{}.txt'.format(startFrame))

                self.LogInfo('Looking for task file: {}'.format(taskFilePath))

                # Wait until task file has been synchronized.
                line = self.WaitForCommandFile(taskFilePath, False, self.taskFileTimeout)
                if not line:
                    self.FailRender('Task file not found at {}'.format(taskFilePath))

                executable = None
                arguments = ''

                try:
                    # Load the task file's data as json dict and process properties
                    json_obj = json.loads(line)

                    executable = RepositoryUtils.CheckPathMapping(json_obj['executable'].replace( "\"", "" ))
                    arguments = RepositoryUtils.CheckPathMapping(json_obj['arguments'])
                except:
                    self.FailRender('Unable to parse task file as json\n\t {}'.format(traceback.format_exc(1)))

            job = self.GetJob()

            work_item_path = None

            # Set the PDG work item environment variables from the task file.
            # Note that this needs to be done before the SetProcessEnvironmentVariable that
            # come after since those should properly override the work item env vars.
            if json_obj and 'pdg_item_env' in json_obj:
                pdg_item_env = json_obj['pdg_item_env']
                try:
                    for key, var in pdg_item_env.items():
                        if key == 'HOUDINI_PATH':
                            # Instead of stomping HOUDINI_PATH we prepend it
                            var_local = os.environ.get(key, '')
                            if var_local:
                                var = var.replace(';&', '')
                                var = var + path_combine + var_local
                        # null json value mean unsetenv
                        if var is None:
                            self.SetProcessEnvironmentVariable(str(key), None)
                        else:
                            self.SetProcessEnvironmentVariable(str(key), str(var))

                    if 'PATH' in pdg_item_env:
                        work_item_path = pdg_item_env['PATH'].replace('__PDG_PATHSEP__', path_combine)
                        self.LogInfo('Changing work item PATH {0} to {1}'.format(pdg_item_env['PATH'], work_item_path))
                except:
                    self.LogWarning('Exception when trying to set env var from task!\n\t {}'.format(traceback.format_exc(1)))

            # Have to add $HFS to sys path otherwise dll loading issues when
            # importing python modules
            hfs_bin = None
            hfs_env = job.GetJobEnvironmentKeyValue('PDG_HFS')
            if not hfs_env:
                self.LogWarning(
                    "$PDG_HFS not found in job environment. "
                    "Houdini jobs might fail.")
                hfs_env = ''
            else:
                # Evaluate HFS to local and append to end of PATH
                hfs_env = RepositoryUtils.CheckPathMapping(hfs_env)
                hfs_bin = hfs_env + '/bin'

            # Set PATH environment if it was set in work item environment and/or
            # if HFS was found in the environment.
            # Note that work item environment PATH takes precendance over local PATH.
            set_path = None
            if hfs_bin:
                if work_item_path:
                    set_path = '{}{}{}'.format(work_item_path, path_combine, hfs_bin)
                    self.LogInfo('Setting work item PATH with HFS: {}'.format(set_path))
                elif 'PATH' in os.environ:
                    set_path = '{}{}{}'.format(os.environ['PATH'], path_combine, hfs_bin)
                    self.LogInfo('Setting os PATH with HFS: {}'.format(set_path))
                else:
                    set_path = hfs_bin
            elif work_item_path:
                set_path = work_item_path
                self.LogInfo('Setting work item PATH: {}'.format(set_path))

            if set_path:
                self.SetProcessEnvironmentVariable('PATH', set_path)

            if os_type == 'windows':
                # Replace {exe} with .exe on Windows since Deadline requires the extension
                executable = executable.replace('$PDG_EXE', '.exe')
            elif os_type == 'macos':

                # No need for $PDG_EXE on this plaform
                executable = executable.replace('$PDG_EXE', '')

                if hfs_env:
                    # Append $PYTHONPATH if not set
                    houdini_python_libs = hfs_env + '/Frameworks/Houdini.framework' \
                        '/Versions/Current/Resources/houdini/python2.7libs'
                    python_path = self.GetProcessEnvironmentVariable('PYTHONPATH')
                    if python_path:
                        if houdini_python_libs not in python_path:
                            python_path.append(path_combine + houdini_python_libs)
                    else:
                        python_path = houdini_python_libs

                    self.LogInfo('Setting PYTHONPATH: {}'.format(python_path))
                    self.SetProcessEnvironmentVariable('PYTHONPATH', python_path)
            else:
                # linux platform

                # No need for $PDG_EXE on this plaform
                executable = executable.replace('$PDG_EXE', '')

            if executable == '$HYTHON' and hfs_env:
                # Map Hython for local platform
                if os_type == 'macos':
                    executable = executable.replace('$HYTHON', hfs_env +
                                                    '/Frameworks/Houdini.framework/Versions/Current/Resources/bin/hython')
                elif os_type == 'windows':
                    executable = executable.replace('$HYTHON', hfs_env + '/bin/hython.exe')
                elif os_type == 'linux':
                    executable = executable.replace('$HYTHON', hfs_env + '/bin/hython')
                self.LogInfo('$HYTHON mapped to: {}'.format(executable))

            arguments = arguments.replace('\\', '/')

            # Evaluate all PDG job vars
            self.ConvertEnv(job, 'PDG_TEMP')
            self.ConvertEnv(job, 'PDG_SHARED_TEMP')
            self.ConvertEnv(job, 'PDG_SCRIPTDIR')
            self.ConvertEnv(job, 'PDG_DIR')
            self.ConvertEnv(job, 'PDG_HFS')
            self.ConvertEnv(job, 'PYTHON')

            # Convert PDG_JOBID to actual job ID
            self.SetProcessEnvironmentVariable('PDG_JOBID', job.JobId)

            # Set batch name which is used when using SubmitAsJob
            self.SetProcessEnvironmentVariable("PDG_JOB_BATCH_NAME", str(job.JobBatchName))

            submit_as_job = False

            if json_obj:
                # Evaluate work item specific PDG vars
                self.SetPDGEnvFromJson(json_obj, 'pdg_item_name', 'PDG_ITEM_NAME')
                self.SetPDGEnvFromJson(json_obj, 'pdg_index', 'PDG_INDEX')
                self.SetPDGEnvFromJson(json_obj, 'pdg_item_id', 'PDG_ITEM_ID')

                submit_as_job = json_obj.get('submit_as_job', 'False') == 'True'
                self.LogInfo("Submit as job: {}".format(submit_as_job))
                
                # Set for making the subsequent job use local MQ
                self.SetProcessEnvironmentVariable("PDG_SUBMIT_AS_JOB", str(submit_as_job))

                if not submit_as_job:

                    # The HTTP port might not be set when using CommandServer,
                    # so force waiting for the connection file in that case.
                    env_httpport = job.GetJobEnvironmentKeyValue( 'PDG_HTTP_PORT' )
                    if env_httpport == 'None':
                        env_httpport = None

                    if not json_obj.get('pdg_result_server', None) or not env_httpport:
                        # Wait for connection file since we don't have server address
                        mq_conn_file_path = job.GetJobEnvironmentKeyValue( 'PDG_MQ_CONN_FILE' )
                        mq_conn_file_path = RepositoryUtils.CheckPathMapping( mq_conn_file_path )
                        self.LogInfo('Waiting for MQ connection file {}'.format(mq_conn_file_path))
                        mqinfo = self.WaitForCommandFile(mq_conn_file_path, False, self.taskFileTimeout)
                        if not mqinfo:
                            self.FailRender('Timed out waiting for MQ connection file at {}'.format(mq_conn_file_path))

                        parsed = self.ParseMQStdout(mqinfo)
                        if parsed:
                            host, rpcport, _, httpport = parsed
                            json_obj['pdg_result_server'] = '{}:{}'.format(host, rpcport)
                            json_obj['pdg_http_port'] = httpport

                    self.SetPDGEnvFromJson(json_obj, 'pdg_result_server', 'PDG_RESULT_SERVER')
                    self.LogInfo('PDG_RESULT_SERVER: {}'.format(json_obj.get('pdg_result_server', None)))

                    self.SetPDGEnvFromJson(json_obj, 'pdg_http_port', 'PDG_HTTP_PORT')
                    self.LogInfo('PDG_HTTP_PORT: {}'.format(json_obj.get('pdg_http_port', 0)))
                
                # Set GPU overrides if specified and valid for this worker
                gpu_list = self.GetGpuOverrides(json_obj)
                if gpu_list and len(gpu_list) > 0:
                    gpus = ",".join(gpu_list)

                    # Set the gpus argument for rop.py which does the render
                    if arguments.find('rop.py') >= 0:
                        self.SetProcessEnvironmentVariable("HOUDINI_GPU_LIST",
                                                           gpus)

                    # Set OpenCL override in environment
                    if json_obj.get('opencl_forcegpu', 0) > 0:
                        self.SetProcessEnvironmentVariable("HOUDINI_OCL_DEVICETYPE", "GPU")
                        self.SetProcessEnvironmentVariable("HOUDINI_OCL_VENDOR", "")
                    self.SetProcessEnvironmentVariable("HOUDINI_OCL_DEVICENUMBER",
                            gpu_list[self.GetThreadNumber() % len(gpu_list)])

            self.LogInfo('Task Executable: %s' % executable)
            self.LogInfo('Task Arguments: %s' % arguments)

            self.LogInfo("Invoking: Run Managed Process")
            self.wProcess = WorkItemProcess(self, executable, arguments, startupDir)
            self.RunManagedProcess(self.wProcess)
            exitCode = self.wProcess.ExitCode
            # exitCode = self.RunProcess( executable, arguments, startupDir, -1 )

            if exitCode != 0:
                # In some situations, user wants to ignore the exit code (such as with 3dscmd)
                if json_obj and json_obj.get('dl_ignoreexit', 0) == 0:
                    self.FailRender('Process returned non-zero exit code: {}'.format(exitCode))
                else:
                    self.LogWarning('Process returned non-zero exit code: {}'.format(exitCode))
        except:
            self.forceKillMonitor = True
            self.FailRender('PDGDeadline exception: {}'.format(traceback.format_exc(1)))

    def GetStartupDirectory(self):
        startupDir = self.GetPluginInfoEntryWithDefault('StartupDirectory', '').strip()
        if startupDir != '':
            startupDir = RepositoryUtils.CheckPathMapping(startupDir)
            self.LogInfo('Startup Directory: %s' % startupDir)
        return startupDir


    def ConvertEnv( self, job, varname ):
        """
        Updates given job's varname environment value with converted mapping.
        The converted value should have been set in the Mapped Paths
        of Deadline's Repository Options .
        """
        incomingEnvVar = job.GetJobEnvironmentKeyValue( varname )
        convertedEnvVar = RepositoryUtils.CheckPathMapping( incomingEnvVar )
        self.SetProcessEnvironmentVariable( varname, convertedEnvVar )

    def SetPDGEnvFromJson(self, json_obj, json_key, pdg_key):
        if json_key in json_obj:
            self.SetProcessEnvironmentVariable( pdg_key, str(json_obj[json_key]) )

    def GetGpuOverrides(self, json_obj):
        """
        Return list of gpu IDs that should be used for this task.
        """
        resultGPUs = []

        # If the number of gpus per task is set, then need to calculate the gpus to use.
        gpusPerTask = json_obj.get("gpus_per_task", 0)
        gpusSelectDevices = json_obj.get("gpus_select_devices", "")

        # Check slave's GPU override to match those specified by user for this job
        if self.OverrideGpuAffinity():
            overrideGPUs = self.GpuAffinity()
            if gpusPerTask == 0 and gpusSelectDevices != "":
                gpus = gpusSelectDevices.split( "," )
                notFoundGPUs = []
                for gpu in gpus:
                    if int( gpu ) in overrideGPUs:
                        resultGPUs.append( gpu )
                    else:
                        notFoundGPUs.append( gpu )

                if len( notFoundGPUs ) > 0:
                    self.LogWarning( "The Slave is overriding its GPU affinity and the following GPUs do not match the Slaves affinity so they will not be used: " + ",".join( notFoundGPUs ) )
                if len( resultGPUs ) == 0:
                    self.FailRender( "The Slave does not have affinity for any of the GPUs specified in the job." )
            elif gpusPerTask > 0:
                if gpusPerTask > len( overrideGPUs ):
                    self.LogWarning( "The Slave is overriding its GPU affinity and the Slave only has affinity for " + str( len( overrideGPUs ) ) + " gpus of the " + str( gpusPerTask ) + " requested." )
                    resultGPUs =  [ str( gpu ) for gpu in overrideGPUs ]
                else:
                    resultGPUs = [ str( gpu ) for gpu in overrideGPUs if gpu < gpusPerTask ]
            else:
                resultGPUs = [ str( gpu ) for gpu in overrideGPUs ]
        elif gpusPerTask == 0 and gpusSelectDevices != "":
            resultGPUs = gpusSelectDevices.split( "," )

        elif gpusPerTask > 0:
            gpuList = []
            #self.LogInfo("!!! GPU: " + str(( self.GetThreadNumber() * gpusPerTask )) + " to " + str(( self.GetThreadNumber() * gpusPerTask ) + gpusPerTask))
            for i in range( ( self.GetThreadNumber() * gpusPerTask ), ( self.GetThreadNumber() * gpusPerTask ) + gpusPerTask ):
                gpuList.append( str( i ) )
            resultGPUs = gpuList

        resultGPUs = list( resultGPUs )
        return resultGPUs

    def EndJob(self):
        """
        Callback when job has finished.
        Do clean up such as shut down the monitor program if its running.
        """
        if self.monitorProgramName and self.MonitoredProgramIsRunning(self.monitorProgramName):
            # The monitor program is still running. Check the job info timeout
            # to see whether to wait with a timeout or indefinitely.
            # During the wait, check if the timeout has changed or job failed in
            # case the monitor program might have to be force killed.
            # Timeout of -1 means to wait indefinitely, which is required
            # for the MQ server. This EndJobCallback can be called prematurately
            # due to the dynamic cooking nature of PDG (where Deadline thinks
            # the job is finished because there are no tasks to schedule but
            # PDG is still cooking and waiting for tasks to finish before
            # scheduling more tasks).

            self.LogInfo('{} is running. Waiting for it to shutdown'.
                         format(self.monitorProgramName))

            while self.MonitoredProgramIsRunning(self.monitorProgramName):
                killMonitor = False
                timeout = self.monitorShutDownTimeout

                # Query the job data freshly instead of the cached version.
                job = self.GetJob()
                if job:
                    job = RepositoryUtils.GetJob(job.JobId, True)

                if job:
                    # Get the MonitorTimeoutOverride which might have been updated
                    #self.LogInfo('MonitorTimeoutOverride: {}'.format \
                    #    (job.GetJobExtraInfoKeyValue('MonitorTimeoutOverride')))
                    try:
                        timeout = int(job.GetJobExtraInfoKeyValue('MonitorTimeoutOverride'))
                    except ValueError:
                        timeout = self.monitorShutDownTimeout
                else:
                    self.LogInfo('No job running so killing monitor!')
                    killMonitor = True

                if killMonitor or self.IsCanceled() or self.forceKillMonitor:
                    self.LogInfo('Shutting down {}'.format(self.monitorProgramName))
                    self.ShutdownMonitoredProgram(self.monitorProgramName)
                    break
                elif timeout >= 0:
                    self.LogInfo('Waiting {} ms then shutting down {}'.format(
                            timeout, self.monitorProgramName))
                    self.WaitForMonitoredProgramToExit(self.monitorProgramName, timeout)
                    break

                SystemUtils.Sleep(500)

            self.LogInfo('{} has finished.'.format(self.monitorProgramName))
        self.monitorProgramName = None

    def MonitoredManagedProgramExit(self, name):
        self.LogInfo('Monitor program {} has exited'.format(name))

    def RunPDGMonitorProgram(self, json_obj, os_type):
        """
        Start running the monitor program.
        Waits for MQ connection file if the monitor program is a MQ server.
        """

        # Get the monitor program arguments from task info
        monitor_exe = RepositoryUtils.CheckPathMapping(json_obj['monitor_exe'].replace( "\"", "" ))
        monitor_args = RepositoryUtils.CheckPathMapping(json_obj['monitor_args'])

        # Timeout for shutting down the monitor program
        self.monitorShutDownTimeout = json_obj.get('monitor_end_timeout', self.monitorShutDownTimeout)

        # Flags whether this is the MQ server
        monitor_mq = json_obj.get('mq_server', False)
        # The MQ server writes out a connection file
        mq_conn_file = RepositoryUtils.CheckPathMapping(json_obj.get('mq_conn_file', ''))

        if os_type == 'windows':
            # Replace {exe} with .exe on Windows since Deadline requires the extension
            monitor_exe = monitor_exe.replace('$PDG_EXE', '.exe')
        else:
            # No need for $PDG_EXE on this plaform
            monitor_exe = monitor_exe.replace('$PDG_EXE', '')

        self.LogInfo('Starting monitor program: {} {}'.format(monitor_exe, monitor_args))
        self.monitorProgramName = 'PDGMonitor'
        self.StartMonitoredProgram(self.monitorProgramName, monitor_exe, monitor_args, self.GetStartupDirectory())

        # For MQ server, we need to wait for the connection file before running
        # the actual task since the task will need the result server address
        if monitor_mq:
            if not mq_conn_file:
                self.FailRender('No MQ connection file specified! Unable to run task.')
            else:
                start = DateTime.Now.Ticks
                while self.MonitoredProgramIsRunning(self.monitorProgramName):
                    if os.path.exists(mq_conn_file):
                        conn_file = open(mq_conn_file, 'r')
                        conn_info = conn_file.read()
                        conn_file.close()
                        self.LogInfo("Got MQ server info: {}".format(conn_info))
                        parsed = self.ParseMQStdout(conn_info)
                        if parsed:
                            host, rpcport, _, httpport = parsed
                            server_endpoint = (host, rpcport)

                            # Store as part of task info
                            json_obj['pdg_result_server'] = '{}:{}'.format(*server_endpoint)
                            json_obj['pdg_http_port'] = httpport

                        break

                    SystemUtils.Sleep(100)

                    # Time out
                    if TimeSpan.FromTicks(DateTime.Now.Ticks - start).Milliseconds > self.mqFileTimeout:
                        self.FailRender('Timed out waiting for MQ connection file.')
                        break

    @staticmethod
    def ParseMQStdout(txt):
        """
        Parse and return the MQ server's connection info
        """
        m = conn_reg_pdgutilset.search(txt)
        if m:
            return (m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)))
        return None

class WorkItemProcess(ManagedProcess):
    """
    Custom managed process to run in RenderTasks,
    so we can handle output/error and other callbacks in the subprocess.
    """
    deadlinePlugin = None

    def __init__(self, deadlinePlugin, executable, argument, directory):
        self.deadlinePlugin = deadlinePlugin
        self.Executable = executable
        self.Argument = argument
        self.Directory = directory
        self.ExitCode = -1

        self.InitializeProcessCallback += self.InitializeProcess
        self.RenderExecutableCallback += self.RenderExecutable
        self.RenderArgumentCallback += self.RenderArgument
        self.StartupDirectoryCallback += self.StartupDirectory
        self.CheckExitCodeCallback += self.CheckExitCode

    def Cleanup(self):
        for stdoutHandler in self.StdoutHandlers:
            del stdoutHandler.HandleCallback

        del self.InitializeProcessCallback
        del self.RenderExecutableCallback
        del self.RenderArgumentCallback
        del self.StartupDirectoryCallback
        del self.CheckExitCodeCallback

    def InitializeProcess(self):
        self.StdoutHandling = True
        # Ensure child processes are killed and the parent process is terminated on exit
        self.UseProcessTree = True
        self.TerminateOnExit = True

        self.AddStdoutHandlerCallback(
            '.*[Ee]rror: .*'
        ).HandleCallback += self.HandleStdoutError
        self.AddStdoutHandlerCallback(
            '.*ERROR .*'
        ).HandleCallback += self.HandleStdoutError
        self.AddStdoutHandlerCallback(
            '.*Fatal error: Segmentation fault.*'
        ).HandleCallback += self.HandleStdoutError
        self.AddStdoutHandlerCallback(
            '.*(No licenses could be found to run this application).*'
        ).HandleCallback += self.HandleStdoutLicense
        self.AddStdoutHandlerCallback(
            '.*TRACEBACK.*'
        ).HandleCallback += self.HandleStdoutError
        self.AddStdoutHandlerCallback(
            r'.*Progress: (\d+)%.*'
        ).HandleCallback += self.HandleProgress

    def RenderExecutable(self):
        return self.Executable

    def RenderArgument(self):
        return self.Argument

    def HandleStdoutError(self):
        self.deadlinePlugin.FailRender(self.GetRegexMatch(0))

    def HandleStdoutLicense(self):
        self.deadlinePlugin.FailRender(self.GetRegexMatch(1))

    def StartupDirectory(self):
        return self.Directory

    def CheckExitCode(self, exitCode):
        self.ExitCode = exitCode

    def HandleProgress(self):
        progress = float(self.GetRegexMatch(1))
        self.deadlinePlugin.SetProgress(progress)
