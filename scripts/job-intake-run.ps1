
$ErrorActionPreference = "Continue"

$stateFile = "C:\Users\Matty\OpenClaw-Orchestrator\state\jobs-seen.json"
$state = Get-Content $stateFile -Raw | ConvertFrom-Json
$seenIds = @{}
foreach ($j in $state.jobs) { $seenIds[$j.id] = $true }
Write-Output "Loaded $($seenIds.Count) seen job IDs"

$newJobs = [System.Collections.Generic.List[object]]::new()
$errors  = [System.Collections.Generic.List[string]]::new()

function Strip-Html($s) {
    if (-not $s) { return "" }
    $s = [System.Text.RegularExpressions.Regex]::Replace($s, '<[^>]+>', ' ')
    $s = [System.Text.RegularExpressions.Regex]::Replace($s, '\s+', ' ')
    return $s.Trim().Substring(0, [Math]::Min(300, $s.Trim().Length))
}

# ─── SOURCE 1: We Work Remotely (RSS) ───
try {
    $wwrXml = Invoke-RestMethod "https://weworkremotely.com/remote-jobs.rss" -TimeoutSec 25
    $count = 0
    foreach ($item in $wwrXml.rss.channel.item) {
        $rawId = $item.link
        $id = "wwr-$rawId"
        if ($seenIds.ContainsKey($id)) { continue }
        $obj = [PSCustomObject]@{
            id                  = $id
            title               = ([string]$item.title).Trim()
            company             = ""
            location            = "Remote"
            remote              = $true
            pay_min             = $null
            pay_max             = $null
            pay_type            = "unspecified"
            description_summary = Strip-Html([string]$item.description)
            url                 = $rawId
            source              = "WeWorkRemotely"
            fetched_at          = (Get-Date -Format o)
            seen                = $false
            filtered            = $false
            tier                = $null
            resume              = $null
            tags                = ""
        }
        $newJobs.Add($obj)
        $count++
    }
    Write-Output "WWR: $count new listings"
} catch {
    $errors.Add("WWR: $_")
    Write-Output "WWR ERROR: $_"
}

# ─── SOURCE 2: Remote OK (REST) ───
try {
    $rok = Invoke-RestMethod "https://remoteok.com/api" -TimeoutSec 25 -Headers @{"User-Agent"="Mozilla/5.0 (compatible)"}
    $count = 0
    for ($i = 1; $i -lt $rok.Count; $i++) {
        $j = $rok[$i]
        $id = "remoteok-$($j.id)"
        if ($seenIds.ContainsKey($id)) { continue }
        $payMin = $null; $payMax = $null; $payType = "unspecified"
        if ($j.salary_min -and [int]$j.salary_min -gt 0) {
            $payMin = [int]$j.salary_min
            $payMax = [int]$j.salary_max
            $payType = "salary"
        }
        $tagStr = ""
        if ($j.tags) { $tagStr = ($j.tags -join ",") }
        $obj = [PSCustomObject]@{
            id                  = $id
            title               = ([string]$j.position).Trim()
            company             = ([string]$j.company).Trim()
            location            = "Remote"
            remote              = $true
            pay_min             = $payMin
            pay_max             = $payMax
            pay_type            = $payType
            description_summary = Strip-Html([string]$j.description)
            url                 = ([string]$j.url)
            source              = "RemoteOK"
            fetched_at          = (Get-Date -Format o)
            seen                = $false
            filtered            = $false
            tier                = $null
            resume              = $null
            tags                = $tagStr
        }
        $newJobs.Add($obj)
        $count++
    }
    Write-Output "RemoteOK: $count new listings"
} catch {
    $errors.Add("RemoteOK: $_")
    Write-Output "RemoteOK ERROR: $_"
}

# ─── SOURCE 3: Remotive (REST) ───
try {
    $rem = Invoke-RestMethod "https://remotive.com/api/remote-jobs" -TimeoutSec 25
    $count = 0
    foreach ($j in $rem.jobs) {
        $id = "remotive-$($j.id)"
        if ($seenIds.ContainsKey($id)) { continue }
        $payType = "unspecified"
        if ($j.salary -and ([string]$j.salary).Trim() -ne "") { $payType = "salary" }
        $loc = if ($j.candidate_required_location) { [string]$j.candidate_required_location } else { "Worldwide" }
        $tagStr = ""
        if ($j.tags) { $tagStr = ($j.tags -join ",") }
        $obj = [PSCustomObject]@{
            id                  = $id
            title               = ([string]$j.title).Trim()
            company             = ([string]$j.company_name).Trim()
            location            = $loc
            remote              = $true
            pay_min             = $null
            pay_max             = $null
            pay_type            = $payType
            salary_raw          = ([string]$j.salary)
            description_summary = Strip-Html([string]$j.description)
            url                 = ([string]$j.url)
            source              = "Remotive"
            fetched_at          = (Get-Date -Format o)
            seen                = $false
            filtered            = $false
            tier                = $null
            resume              = $null
            tags                = $tagStr
        }
        $newJobs.Add($obj)
        $count++
    }
    Write-Output "Remotive: $count new listings"
} catch {
    $errors.Add("Remotive: $_")
    Write-Output "Remotive ERROR: $_"
}

# ─── SOURCE 4: Adzuna (3 queries, US endpoint) ───
$appId  = "7d62bedb"
$appKey = "0bc5b0c39310dfd5d91fb83c1a43c186"
$adzQueries = @(
    @{ what = "blockchain OR solidity OR web3 OR defi OR AI developer"; salary_min = 60000;  rpp = 50 },
    @{ what = "software developer OR engineer OR technical analyst";     salary_min = 100000; rpp = 50 },
    @{ what = "telecom sales OR customer support OR technical support";  salary_min = 35000;  rpp = 20 }
)
$adzCount = 0
$adzRateLimited = $false
foreach ($q in $adzQueries) {
    if ($adzRateLimited) { break }
    try {
        $what = [uri]::EscapeDataString($q.what)
        $uri = "http://api.adzuna.com/v1/api/jobs/us/search/1?app_id=$appId&app_key=$appKey&what=$what&where=remote&salary_min=$($q.salary_min)&results_per_page=$($q.rpp)&sort_by=date"
        $resp = Invoke-RestMethod $uri -TimeoutSec 25
        foreach ($j in $resp.results) {
            $id = "adzuna-$($j.id)"
            if ($seenIds.ContainsKey($id)) { continue }
            $payMin = $null; $payMax = $null; $payType = "unspecified"
            if ($j.salary_min -and [double]$j.salary_min -gt 0) {
                $payMin = [int]$j.salary_min
                $payType = "salary"
            }
            if ($j.salary_max -and [double]$j.salary_max -gt 0) {
                $payMax = [int]$j.salary_max
            }
            $loc = if ($j.location -and $j.location.display_name) { $j.location.display_name } else { "Remote" }
            $co  = if ($j.company -and $j.company.display_name) { $j.company.display_name } else { "" }
            $obj = [PSCustomObject]@{
                id                  = $id
                title               = ([string]$j.title).Trim()
                company             = $co
                location            = $loc
                remote              = $true
                pay_min             = $payMin
                pay_max             = $payMax
                pay_type            = $payType
                description_summary = Strip-Html([string]$j.description)
                url                 = ([string]$j.redirect_url)
                source              = "Adzuna"
                fetched_at          = (Get-Date -Format o)
                seen                = $false
                filtered            = $false
                tier                = $null
                resume              = $null
                tags                = ""
            }
            $newJobs.Add($obj)
            $adzCount++
        }
        Start-Sleep -Milliseconds 600
    } catch {
        if ([string]$_ -match "429") {
            $adzRateLimited = $true
            $errors.Add("Adzuna: rate limited")
            Write-Output "Adzuna RATE LIMITED - stopping Adzuna queries"
        } else {
            $errors.Add("Adzuna query '$($q.what)': $_")
            Write-Output "Adzuna ERROR for query '$($q.what)': $_"
        }
    }
}
Write-Output "Adzuna: $adzCount new listings"

Write-Output ""
Write-Output "=== TOTAL NEW LISTINGS: $($newJobs.Count) ==="

# Save for next step
$newJobs | ConvertTo-Json -Depth 6 | Set-Content "C:\Users\Matty\OpenClaw-Orchestrator\state\intake-temp.json" -Encoding UTF8
Write-Output "Saved intake-temp.json"

# Log errors
if ($errors.Count -gt 0) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $errorLines = $errors | ForEach-Object { "[$ts] $_" }
    Add-Content "C:\Users\Matty\OpenClaw-Orchestrator\logs\job-intake-errors.log" ($errorLines -join "`n")
    Write-Output "Logged $($errors.Count) errors"
}
