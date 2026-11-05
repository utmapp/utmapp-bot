/* fgforce: keep a fullscreen workload window in the foreground.
 *
 * usage: fgforce "<window title>" <seconds>
 *
 * Passive: it only acts when the window has been out of the foreground for
 * about 3 s, and then only with SetForegroundWindow.  Never inject keys into a
 * fullscreen D3D window (an injected Alt press once left the session's window
 * creation stalling for minutes).
 */
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv)
{
    const char *title = argc > 1 ? argv[1] : "3DMark Workload";
    int hold = argc > 2 ? atoi(argv[2]) : 120;
    DWORD deadline = GetTickCount() + 180000, end;
    HWND w = NULL;
    int forces = 0, streak = 0;

    while (GetTickCount() < deadline && !(w = FindWindowA(NULL, title)))
        Sleep(100);
    if (!w) {
        printf("window '%s' not found\n", title);
        return 1;
    }
    printf("watching hwnd=%p\n", (void *)w);
    fflush(stdout);
    end = GetTickCount() + (DWORD)hold * 1000;
    while (GetTickCount() < end && IsWindow(w)) {
        if (GetForegroundWindow() == w) {
            streak = 0;
        } else if (++streak >= 6) {
            BOOL ok = SetForegroundWindow(w);
            BringWindowToTop(w);
            printf("forced foreground (ok=%d)\n", ok);
            fflush(stdout);
            forces++;
            streak = 0;
        }
        Sleep(500);
    }
    printf("done, intervened %d times\n", forces);
    return 0;
}
