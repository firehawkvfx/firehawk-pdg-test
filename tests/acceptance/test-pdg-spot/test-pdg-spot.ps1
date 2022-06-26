#Requires -Version 7.0

$timeout_mins=15

$ErrorActionPreference = "Stop"

function Main {
    Write-Host "Ensure pdgtemp exists"
    if (-Not (Test-Path X:\temp\pdgtemp -PathType Container)) {
        Write-Host "Create directory"
        New-Item -Path X:\temp\pdgtemp -Type Directory
    }

    Write-Host "Get paths to cleanup before submit..."
    $output = $(Get-ChildItem X:\temp -Include geo,ifds,render -Recurse)
    Write-Host "Cleanup: $output"
    Get-ChildItem X:\temp -Include geo,ifds,render -Recurse | Remove-Item -Recurse

    Write-Host "Copy .hip file"
    Copy-Item -Path $PSScriptRoot\test.deadline.v023.test_pdg_ubl_h19.0_graph_as_job_mantra.hip -Destination X:\temp\. -Force
    Write-Host "Copy temp dir"
    Copy-Item -Path $PSScriptRoot\10360 -Destination X:\temp\pdgtemp\. -Recurse -Force

    Write-Host "`nSubmitting..."
    $allOutput = $(& 'C:\Program Files\Thinkbox\Deadline10\bin\deadlinecommand.exe' `
        $PSScriptRoot\deadline-job-files\62b7fb682946471ad77efd2d_jobInfo.job `
        $PSScriptRoot\deadline-job-files\62b7fb682946471ad77efd2d_pluginInfo.job 2>&1)

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

    $startDate = Get-Date
    $update_time = Get-Date

    while ((-Not ($completed -eq 1)) -And ($failed -lt 1) -And ($startDate.AddMinutes($timeout_mins) -gt $update_time)) {
        Write-Host "`nGetting job details until job passes or fails"
        $jobdetails = $(& 'C:\Program Files\Thinkbox\Deadline10\bin\deadlinecommand.exe' -GetJobDetails $jobid)
        $completed = Get-Value "$jobdetails" "Completed"
        $failed = Get-Value "$jobdetails" "Failed"
        $pending = Get-Value "$jobdetails" "Pending"
        $rendering = Get-Value "$jobdetails" "Rendering"
        $suspended = Get-Value "$jobdetails" "Suspended"
        Start-Sleep -s 10
        $update_time = Get-Date
    }

    if (-Not ($startDate.AddMinutes($timeout_mins) -gt $update_time)) {
        Write-Warning "Timed out after $timeout_mins mins"
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
