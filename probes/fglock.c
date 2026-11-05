/* fglock: set the session's foreground-lock timeout to 0.
 *
 * A test VM has no interactive user whose focus needs protecting.  With the
 * default 200 s lock, a display mode switch's synthetic input makes win32k
 * refuse the next fullscreen app's foreground request: DXGI then silently
 * keeps the window composed (windowed) and the app runs at about half its
 * frame rate.  Run once per boot, in the console session (utmbot install
 * does).
 */
#include <windows.h>
#include <stdio.h>

int main(void)
{
    DWORD before = 0, after = 0;
    BOOL ok;

    SystemParametersInfoA(SPI_GETFOREGROUNDLOCKTIMEOUT, 0, &before, 0);
    ok = SystemParametersInfoA(SPI_SETFOREGROUNDLOCKTIMEOUT, 0, NULL,
                               SPIF_UPDATEINIFILE | SPIF_SENDCHANGE);
    SystemParametersInfoA(SPI_GETFOREGROUNDLOCKTIMEOUT, 0, &after, 0);
    printf("foreground lock timeout: %lums -> %lums (set ok=%d)\n", before, after, ok);
    return (ok && after == 0) ? 0 : 1;
}
