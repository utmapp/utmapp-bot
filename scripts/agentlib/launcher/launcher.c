// Runs argv[1..] as a child and waits, so this signed bundle stays the
// responsible process (macOS privacy grants such as Local Network attach to it).
#include <spawn.h>
#include <signal.h>
#include <stdio.h>
#include <sys/wait.h>
#include <unistd.h>
extern char **environ;
static pid_t child;
static void fwd(int s) { if (child > 0) kill(child, s); }
int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: %s program [args...]\n", argv[0]); return 64; }
    signal(SIGTERM, fwd); signal(SIGINT, fwd); signal(SIGHUP, fwd);
    int e = posix_spawnp(&child, argv[1], NULL, NULL, argv + 1, environ);
    if (e) { fprintf(stderr, "%s: cannot run %s: errno %d\n", argv[0], argv[1], e); return 127; }
    int st;
    while (waitpid(child, &st, 0) < 0) {}
    if (WIFEXITED(st)) return WEXITSTATUS(st);
    return 128 + WTERMSIG(st);
}
