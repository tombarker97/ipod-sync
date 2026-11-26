/*
 * iPod Sync launcher.
 *
 * This is the whole "app": it starts ~/.ipod-sync/ipod-sync.sh and waits for
 * it to finish. It exists so that macOS attributes the sync's access to the
 * iPod to "iPod Sync.app" - one "Removable Volumes" permission for this app -
 * instead of needing Full Disk Access for bash and Python.
 *
 * It must stay running while the script runs (it is the "responsible
 * process" macOS checks), so it spawns the script rather than exec'ing it.
 */
#include <errno.h>
#include <pwd.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

int main(int argc, char *argv[])
{
    const char *home = getenv("HOME");
    if (!home || !*home) {
        struct passwd *pw = getpwuid(getuid());
        if (!pw) return 70;
        home = pw->pw_dir;
    }

    char script[4096];
    snprintf(script, sizeof script, "%s/.ipod-sync/ipod-sync.sh", home);

    char **args = calloc((size_t)argc + 2, sizeof(char *));
    if (!args) return 70;
    args[0] = "/bin/bash";
    args[1] = script;
    for (int i = 1; i < argc; i++) args[i + 1] = argv[i];

    pid_t pid;
    int rc = posix_spawn(&pid, "/bin/bash", NULL, NULL, args, environ);
    if (rc != 0) {
        fprintf(stderr, "iPod Sync: could not start %s (error %d)\n", script, rc);
        return 71;
    }

    int status;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) return 72;
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
