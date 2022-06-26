#Requires -Version 7.0

param (
    [parameter(mandatory=$true)][ValidateSet("windows","linux")][string]$os_type,
    [parameter(mandatory=$false)][int]$timeout_mins = 20
)

$ErrorActionPreference = "Stop"

function Main {
    if ($os_type -eq "windows") {
        $test_temp_path = "X:\temp"
        $test_pdgtemp_path = "$test_temp_path\pdgtemp"
        $test_deadline_command_path = "C:\Program Files\Thinkbox\Deadline10\bin\deadlinecommand.exe"
        $jobinfo = "$PSScriptRoot\deadline-job-files\62b7fb682946471ad77efd2d_jobInfo.job"
        $plugininfo = "$PSScriptRoot\deadline-job-files\62b7fb682946471ad77efd2d_pluginInfo.job"
    } elseif ($os_type -eq "linux") {
        $test_temp_path = "/Volumes/cloud_prod/temp"
        $test_pdgtemp_path = "$test_temp_path/pdgtemp"
        $test_deadline_command_path = "/opt/Thinkbox/Deadline10/bin/deadlinecommand"
        $jobinfo = "$PSScriptRoot/deadline-job-files/62b7fb682946471ad77efd2d_jobInfo.job"
        $plugininfo = "$PSScriptRoot/deadline-job-files/62b7fb682946471ad77efd2d_pluginInfo.job"
    } else {
        throw "NO OS specified."
    }

    Write-Host "Ensure pdgtemp exists"
    if (-Not (Test-Path $test_pdgtemp_path -PathType Container)) {
        Write-Host "Create directory"
        New-Item -Path $test_pdgtemp_path -Type Directory
    }

    Write-Host "Get paths to cleanup before submit..."
    $output = $(Get-ChildItem $test_temp_path -Include geo,ifds,render -Recurse)
    Write-Host "Cleanup: $output"
    Get-ChildItem $test_temp_path -Include geo,ifds,render -Recurse | Remove-Item -Recurse

    Write-Host "Copy .hip file"
    Copy-Item -Path $PSScriptRoot\test.deadline.v023.test_pdg_ubl_h19.0_graph_as_job_mantra.hip -Destination $test_temp_path -Force
    Write-Host "Copy temp dir"
    Copy-Item -Path $PSScriptRoot\10360 -Destination $test_pdgtemp_path -Recurse -Force

    Write-Host "`nSubmitting..."
    $allOutput = $(& "$test_deadline_command_path" $jobinfo $plugininfo 2>&1)

    $stderr = $allOutput | ?{ $_ -is [System.Management.Automation.ErrorRecord] }
    $output = $allOutput | ?{ $_ -isnot [System.Management.Automation.ErrorRecord] }

    if ($LASTEXITCODE -eq 0) {
        Write-Host $output
    } else {
        Write-Warning "Submission failed"
        Write-Warning "STDERR: $stderr"
        Write-Warning "STDOUT: $output"
    }

    Write-Host "`nFilter JOBID from output:"
    # $jobid = "$output" | Select-String -Pattern 'JobID=([\d\w]*)\s' | % {"JOBID is $($_.matches.groups[1])"}
    $jobid = $("$output" | Select-String -Pattern 'JobID=([\d\w]*)\s').matches.groups[1]
    Write-Host "JOBID: $jobid"

    $completed = 0
    $failed = 0

    function Get-Value {
        param (
            [parameter(mandatory=$true)][string]$jobdetails,
            [parameter(mandatory=$true)][string]$name
        )
        $returnedvalue = [int]"$($(`"$jobdetails`" | Select-String -Pattern `"${name}:([\d]*)`").matches.groups[1])"
        Write-Host "$name $returnedvalue"
        $returnedvalue
    }

    Write-Host "`nMonitoring job until success or failure..."

    $startTime = Get-Date
    $elapsedTime = $(get-date) - $StartTime

    while ((-Not ($completed -eq 1)) -And ($failed -lt 1) -And ($startTime.AddMinutes($timeout_mins) -gt $($startTime + $elapsedTime))) {
        Write-Host "`nGetting job details until job passes or fails."
        $jobdetails = $(& "$test_deadline_command_path" -GetJobDetails $jobid)
        $completed = Get-Value "$jobdetails" "Completed"
        $failed = Get-Value "$jobdetails" "Failed"
        $pending = Get-Value "$jobdetails" "Pending"
        $rendering = Get-Value "$jobdetails" "Rendering"
        $suspended = Get-Value "$jobdetails" "Suspended"
        $elapsedTime = $(get-date) - $StartTime
        $totalTime = "{0:HH:mm:ss}" -f ([datetime]$elapsedTime.Ticks)
        Write-Host "Elapsed Time $totalTime"
        Start-Sleep -s 10
    }

    if (-Not ($startTime.AddMinutes($timeout_mins) -gt $($startTime + $elapsedTime))) {
        Write-Warning "Timed out after $totalTime"
        Write-Host "`nJob details:"
        Write-Host $jobdetails
        exit(1)
    }

    if ($failed -gt 0) {
        Write-Warning "Job Failed. Failed: $failed"
        Write-Host "`nJob details:"
        Write-Host $jobdetails
        exit(1)
    }

    if (-Not ($completed -eq 1)) {
        Write-Warning "Job Failed. Completed: $completed"
        Write-Host "`nJob details:"
        Write-Host $jobdetails
        exit(1)
    }

    Write-Host "`nTest Success"
    Write-Host "Duration: $totalTime"
}

Main

# deadlinecommand -GetJobLogReportFilenames <Job ID>

# JobCompletedTasks=0
# JobFailedTasks=0
# ErrorReports=0

# Completed:0
# Failed:0
# Pending:0
# Queued:0
# Rendering:1
# Suspended:0
