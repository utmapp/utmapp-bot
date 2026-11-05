# Reset the display mode from the interactive session (ChangeDisplaySettings
# with CDS_RESET), which re-applies the current mode through the driver.
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class Cds {
    [DllImport("user32.dll")]
    public static extern int ChangeDisplaySettingsW(IntPtr devMode, uint flags);
}
"@
$rc = [Cds]::ChangeDisplaySettingsW([IntPtr]::Zero, 0x40000000)
Write-Output "ChangeDisplaySettings(CDS_RESET) = $rc"
