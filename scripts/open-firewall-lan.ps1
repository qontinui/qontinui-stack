# Open Windows Firewall for qontinui-stack services, scoped to the local LAN subnet.
#
# Run from an ELEVATED PowerShell prompt (Run as Administrator). Required because
# New-NetFirewallRule needs admin privileges.
#
# Scope: only RemoteAddress 192.168.178.0/24 (the LAN subnet). Public-network
# traffic is still blocked. Adjust the $LanSubnet line if your LAN uses a
# different subnet.
#
# Idempotent: re-running removes-and-recreates each rule so changes apply
# cleanly.
#
# To remove all rules later:
#   Get-NetFirewallRule -Name "qontinui-stack-*" | Remove-NetFirewallRule

$ErrorActionPreference = "Stop"

# Confirm we are elevated.
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "This script must run from an elevated (Administrator) PowerShell prompt."
    exit 1
}

$LanSubnet = "192.168.178.0/24"

$rules = @(
    @{ Name = "qontinui-stack-postgres-5433"; Port = 5433; DisplayName = "qontinui-stack canonical Postgres (5433/tcp)" },
    @{ Name = "qontinui-stack-redis-6380";    Port = 6380; DisplayName = "qontinui-stack canonical Redis (6380/tcp)" },
    @{ Name = "qontinui-stack-object-store-9100"; Port = 9100; DisplayName = "qontinui-stack canonical object store S3 API (9100/tcp)" },
    @{ Name = "qontinui-stack-coord-9870";    Port = 9870; DisplayName = "qontinui-stack coord service (9870/tcp)" }
)

# Rules for ports the stack no longer serves. The object store that replaced
# MinIO (plan 2026-11-22-fetches-stop-depending-on-upstreams) has no console,
# so 9101 is closed again rather than left open to nothing; 9100 keeps its
# port under the new rule name above.
$retiredRules = @("qontinui-stack-minio-9100", "qontinui-stack-minio-9101")
foreach ($name in $retiredRules) {
    if (Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue) {
        Get-NetFirewallRule -Name $name | Remove-NetFirewallRule
        Write-Host ("DEL {0,-32}  (retired)" -f $name)
    }
}

foreach ($r in $rules) {
    Get-NetFirewallRule -Name $r.Name -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule `
        -Name $r.Name `
        -DisplayName $r.DisplayName `
        -Direction Inbound `
        -Protocol TCP `
        -LocalPort $r.Port `
        -Action Allow `
        -RemoteAddress $LanSubnet `
        -Profile Private,Domain | Out-Null
    Write-Host ("OK  {0,-32}  TCP {1}  remote={2}" -f $r.Name, $r.Port, $LanSubnet)
}

Write-Host ""
Write-Host "Done. Verify from another LAN machine:"
Write-Host "  Test-NetConnection 192.168.178.112 -Port 5433"
Write-Host "  Test-NetConnection 192.168.178.112 -Port 6380"
Write-Host "  Test-NetConnection 192.168.178.112 -Port 9100"
