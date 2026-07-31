param(
    [string]$BundlePath = ""
)

$ErrorActionPreference = "Stop"

if (-not $BundlePath) {
    $BundlePath = Join-Path $PSScriptRoot "..\build\bridge\TrafficSuperVisionBridge"
}
$bundle = (Resolve-Path -LiteralPath $BundlePath).Path
$executable = Join-Path $bundle "TrafficSuperVisionBridge.exe"
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "Frozen bridge executable is missing"
}

$temporaryParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$smokeRoot = Join-Path $temporaryParent (
    "TrafficSuperVisionFrozenSmoke-" + [Guid]::NewGuid().ToString("N")
)
New-Item -ItemType Directory -Path $smokeRoot | Out-Null

$token = "frozen-smoke-token-with-at-least-256-bits-0123456789abcdef"
$startInfo = [Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = $executable
$startInfo.WorkingDirectory = $bundle
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
$startInfo.Environment["TRAFFICSUPERVISION_BRIDGE_TOKEN"] = $token
$startInfo.Environment["LOCALAPPDATA"] = $smokeRoot
$startInfo.Environment["PATH"] = (
    $bundle + [IO.Path]::PathSeparator + $startInfo.Environment["PATH"]
)
$null = $startInfo.Environment.Remove("TRAFFICSUPERVISION_VISION_MODEL_PACK")
$null = $startInfo.Environment.Remove(
    "TRAFFICSUPERVISION_EXTERNAL_MODEL_RUNTIME_PACK"
)
$null = $startInfo.Environment.Remove(
    "TRAFFICSUPERVISION_DFINE_TRAINING_RUNTIME"
)
$null = $startInfo.Environment.Remove(
    "TRAFFICSUPERVISION_DFINE_TRAINING_RUNTIME_SHA256"
)

$process = [Diagnostics.Process]::new()
$process.StartInfo = $startInfo
try {
    if (-not $process.Start()) {
        throw "Frozen bridge did not start"
    }
    $readinessTask = $process.StandardOutput.ReadLineAsync()
    if (-not $readinessTask.Wait(20000)) {
        throw "Frozen bridge readiness timed out"
    }
    $readinessLine = $readinessTask.Result
    $prefix = "TRAFFICSUPERVISION_BRIDGE_READY "
    if (-not $readinessLine.StartsWith($prefix)) {
        $safeError = $process.StandardError.ReadToEnd()
        if ($safeError.Length -gt 500) {
            $safeError = $safeError.Substring(0, 500)
        }
        throw "Frozen bridge readiness was invalid: $safeError"
    }
    $ready = $readinessLine.Substring($prefix.Length) | ConvertFrom-Json
    if (
        $ready.host -ne "127.0.0.1" -or
        $ready.protocol -ne "http" -or
        $ready.port -lt 1 -or
        $ready.port -gt 65535
    ) {
        throw "Frozen bridge readiness payload failed validation"
    }

    $headers = @{ Authorization = "Bearer $token" }
    $baseUrl = "http://127.0.0.1:$($ready.port)"
    $health = Invoke-RestMethod `
        -Uri "$baseUrl/health" `
        -Headers $headers `
        -Method Get `
        -TimeoutSec 10
    $projects = Invoke-RestMethod `
        -Uri "$baseUrl/projects" `
        -Headers $headers `
        -Method Get `
        -TimeoutSec 10
    if (
        $health.status -ne "ok" -or
        $health.fixture_mode -ne $false -or
        $projects.projects.Count -lt 1
    ) {
        throw "Frozen production bridge API smoke failed"
    }

    # Exercise the same opaque local-file -> SHA-256 -> analysis path used by
    # Electron main. This proves the copied FFmpeg/FFprobe binaries work after
    # relocation and that a no-model-pack build returns an honest scene
    # fallback instead of an invented road-defect class.
    $mediaRoot = Join-Path $smokeRoot "media"
    New-Item -ItemType Directory -Path $mediaRoot | Out-Null
    $videoPath = Join-Path $mediaRoot "frozen-smoke.mp4"
    $ffmpeg = Join-Path $bundle "ffmpeg.exe"
    $encoderListing = & $ffmpeg -hide_banner -encoders 2>&1
    if (
        $LASTEXITCODE -ne 0 -or
        ($encoderListing -join "`n") -notmatch "(?m)^\s*V\S*\s+libx264\s"
    ) {
        throw "Bundled FFmpeg does not expose the required libx264 encoder"
    }
    $generator = Start-Process `
        -FilePath $ffmpeg `
        -ArgumentList @(
            "-hide_banner",
            "-loglevel", "error",
            "-f", "lavfi",
            "-i", "testsrc2=size=160x90:rate=10:duration=2",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "28",
            "-pix_fmt", "yuv420p",
            "-y", "`"$videoPath`""
        ) `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
    if ($generator.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $videoPath)) {
        throw "Bundled FFmpeg could not create the smoke video"
    }

    $projectId = [string]$projects.projects[0].id
    $snapshot = Invoke-RestMethod `
        -Uri "$baseUrl/projects/$projectId/media/local-file" `
        -Headers $headers `
        -ContentType "application/json" `
        -Body (@{
            project_id = $projectId
            selected_path = $videoPath
        } | ConvertTo-Json -Compress) `
        -Method Post `
        -TimeoutSec 10
    $hashJob = Invoke-RestMethod `
        -Uri "$baseUrl/projects/$projectId/media/hash-jobs" `
        -Headers $headers `
        -ContentType "application/json" `
        -Body (@{
            project_id = $projectId
            snapshot_id = [string]$snapshot.snapshot_id
            file_name = "frozen-smoke.mp4"
        } | ConvertTo-Json -Compress) `
        -Method Post `
        -TimeoutSec 10
    for ($attempt = 0; $attempt -lt 200; $attempt++) {
        if ($hashJob.status -in @("completed", "failed", "cancelled")) {
            break
        }
        Start-Sleep -Milliseconds 100
        $hashJob = Invoke-RestMethod `
            -Uri (
                "$baseUrl/projects/$projectId/media/hash-jobs/" +
                [string]$hashJob.job_id
            ) `
            -Headers $headers `
            -Method Get `
            -TimeoutSec 10
    }
    if ($hashJob.status -ne "completed") {
        throw "Frozen bridge SHA-256 job did not complete"
    }

    $analysis = Invoke-RestMethod `
        -Uri "$baseUrl/projects/$projectId/analysis/jobs" `
        -Headers $headers `
        -ContentType "application/json" `
        -Body (@{
            project_id = $projectId
            snapshot_id = [string]$snapshot.snapshot_id
            hash_job_id = [string]$hashJob.job_id
            file_name = "frozen-smoke.mp4"
            sha256_lower = [string]$hashJob.sha256_lower
        } | ConvertTo-Json -Compress) `
        -Method Post `
        -TimeoutSec 10
    for ($attempt = 0; $attempt -lt 600; $attempt++) {
        if ($analysis.status -in @("completed", "failed", "cancelled")) {
            break
        }
        Start-Sleep -Milliseconds 100
        $analysis = Invoke-RestMethod `
            -Uri (
                "$baseUrl/projects/$projectId/analysis/jobs/" +
                [string]$analysis.job_id
            ) `
            -Headers $headers `
            -Method Get `
            -TimeoutSec 10
    }
    if (
        $analysis.status -ne "completed" -or
        $analysis.result.candidate_count -lt 1 -or
        $analysis.result.vision_inference.mode -ne "scene_fallback" -or
        $analysis.result.vision_inference.warning_code -ne (
            "model_pack_not_configured"
        )
    ) {
        $diagnostic = [pscustomobject]@{
            status = $analysis.status
            stage = $analysis.stage
            error_code = $analysis.error_code
            candidate_count = $analysis.result.candidate_count
            vision_mode = $analysis.result.vision_inference.mode
            vision_warning = $analysis.result.vision_inference.warning_code
        } | ConvertTo-Json -Compress
        throw (
            "Frozen no-model analysis did not return the expected fallback: " +
            $diagnostic
        )
    }

    # Chromium/Electron cannot decode the previous MPEG-4 Part 2 (`mp4v`)
    # preview output. Verify the frozen production path emits H.264 evidence,
    # rather than merely checking that the encoder happens to be installed.
    $candidateId = [string]$analysis.result.candidates[0].candidate_id
    $previewPath = Join-Path $smokeRoot "frozen-preview.mp4"
    Invoke-WebRequest `
        -Uri (
            "$baseUrl/projects/$projectId/analysis/jobs/" +
            [string]$analysis.job_id +
            "/candidates/$candidateId/clip"
        ) `
        -Headers $headers `
        -Method Get `
        -OutFile $previewPath `
        -TimeoutSec 10 | Out-Null
    $ffprobe = Join-Path $bundle "ffprobe.exe"
    $previewCodecOutput = & $ffprobe `
        -v error `
        -select_streams "v:0" `
        -show_entries "stream=codec_name" `
        -of "default=noprint_wrappers=1:nokey=1" `
        $previewPath 2>$null
    $previewCodec = (
        ($previewCodecOutput | Select-Object -First 1).ToString().Trim()
    )
    if ($LASTEXITCODE -ne 0 -or $previewCodec -ne "h264") {
        throw "Frozen candidate preview is not Chromium-compatible H.264"
    }

    [pscustomobject]@{
        executable = $executable
        loopback = $ready.host
        port_assigned = $true
        health = $health.status
        project_count = $projects.projects.Count
        fixture_mode = $health.fixture_mode
        vision_pack_configured = $false
        analysis_status = $analysis.status
        candidate_count = $analysis.result.candidate_count
        vision_mode = $analysis.result.vision_inference.mode
        vision_warning = $analysis.result.vision_inference.warning_code
        bundled_ffmpeg = $true
        preview_codec = $previewCodec
    } | ConvertTo-Json -Compress
}
finally {
    if (-not $process.HasExited) {
        $process.Kill()
        $null = $process.WaitForExit(5000)
    }
    $resolvedSmokeRoot = [IO.Path]::GetFullPath($smokeRoot)
    if (
        $resolvedSmokeRoot.StartsWith(
            $temporaryParent,
            [StringComparison]::OrdinalIgnoreCase
        ) -and
        [IO.Path]::GetFileName($resolvedSmokeRoot).StartsWith(
            "TrafficSuperVisionFrozenSmoke-"
        )
    ) {
        Remove-Item -LiteralPath $resolvedSmokeRoot -Recurse -Force
    }
}
