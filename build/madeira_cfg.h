/* madeira_cfg.h -- ONE configuration file for every runtime switch (ml1095).
 *
 *   Documents/madeira.cfg
 *     # comments start with #
 *     key = value            (whitespace around key and value is trimmed;
 *                             the value runs to the end of the line, so it
 *                             may itself contain '='; the last line wins)
 *
 * Keys are the old per-file names without the "madeira-" prefix and ".txt"
 * suffix: swap-mb, vram-mb, pool, totalphys, inproc-sync, wx, ...
 * Environment exports are "env.NAME = value" (see madeira_cfg_env_export).
 *
 * Header-only and dependency-free on purpose: it is included from six
 * separately built libraries (ntdll unix, wineserver, madsync, the winemetal
 * bridge, the shader converter, the app) that share no link step. Each call
 * re-reads the file; every caller runs once at start-up and caches its own
 * answer, and a file this small costs nothing.
 *
 * COMPATIBILITY: when madeira.cfg is ABSENT, the legacy Documents/madeira-<key>.txt
 * is consulted, so a device without the new file keeps behaving exactly as
 * before. When madeira.cfg is PRESENT the legacy files are ignored entirely,
 * which is what makes a single file the single source of truth. */
#ifndef MADEIRA_CFG_H
#define MADEIRA_CFG_H

#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define MADEIRA_CFG_FILE "madeira.cfg"
#define MADEIRA_CFG_MAX  (64 * 1024)

static int madeira_cfg__read_file(const char *path, char *buf, size_t cap)
{
    int fd = open(path, O_RDONLY);
    ssize_t total = 0, got;
    if (fd < 0) return -1;
    while (total < (ssize_t)cap - 1 && (got = read(fd, buf + total, cap - 1 - total)) > 0) total += got;
    close(fd);
    buf[total] = '\0';
    return (int)total;
}

static void madeira_cfg__trim(char *s)
{
    size_t n = strlen(s), i = 0;
    while (n && (s[n - 1] == ' ' || s[n - 1] == '\t' || s[n - 1] == '\r' || s[n - 1] == '\n')) s[--n] = '\0';
    while (s[i] == ' ' || s[i] == '\t') i++;
    if (i) memmove(s, s + i, n - i + 1);
}

/* Directory that holds the configuration: $MADEIRA_DOCS_DIR, else $HOME/Documents. */
static int madeira_cfg__dir(char *out, size_t cap)
{
    const char *docs = getenv("MADEIRA_DOCS_DIR");
    if (docs && *docs) { if (strlen(docs) >= cap) return 0; strcpy(out, docs); return 1; }
    docs = getenv("HOME");
    if (!docs || !*docs || strlen(docs) + 11 >= cap) return 0;
    strcpy(out, docs); strcat(out, "/Documents");
    return 1;
}

/* 1 = madeira.cfg exists (legacy files are then ignored). */
static int madeira_cfg_present(void)
{
    char path[1024];
    if (!madeira_cfg__dir(path, sizeof path - 32)) return 0;
    strcat(path, "/" MADEIRA_CFG_FILE);
    return access(path, R_OK) == 0;
}

/* Look `key` up. Returns 1 and the trimmed value in out (may be empty) when the
 * key is set; 0 when it is not (out is then ""). */
static int madeira_cfg_get(const char *key, char *out, size_t cap)
{
    char path[1024];
    char *buf;
    int found = 0;
    size_t klen = strlen(key);

    if (cap) out[0] = '\0';
    if (!madeira_cfg__dir(path, sizeof path - 64)) return 0;

    buf = (char *)malloc(MADEIRA_CFG_MAX);
    if (!buf) return 0;
    strcat(path, "/" MADEIRA_CFG_FILE);
    if (madeira_cfg__read_file(path, buf, MADEIRA_CFG_MAX) >= 0)
    {
        char *line = buf, *next;
        for (; line && *line; line = next)
        {
            char *eq;
            next = strchr(line, '\n');
            if (next) *next++ = '\0';
            madeira_cfg__trim(line);
            if (!*line || *line == '#') continue;
            eq = strchr(line, '=');
            if (!eq) continue;
            *eq = '\0';
            madeira_cfg__trim(line);
            if (strlen(line) != klen || strncmp(line, key, klen)) continue;
            madeira_cfg__trim(eq + 1);
            if (cap) { strncpy(out, eq + 1, cap - 1); out[cap - 1] = '\0'; }
            found = 1;   /* keep going: the last occurrence wins */
        }
        free(buf);
        return found;
    }

    /* No madeira.cfg: the legacy one-value-per-file layout. */
    path[strlen(path) - strlen(MADEIRA_CFG_FILE)] = '\0';
    if (strlen(path) + 9 + klen + 4 < sizeof path)
    {
        strcat(path, "madeira-"); strcat(path, key); strcat(path, ".txt");
        if (madeira_cfg__read_file(path, buf, MADEIRA_CFG_MAX) >= 0)
        {
            char *nl = strchr(buf, '\n');
            /* a legacy file is ONE value; multi-line ones (env, dxmt) have their
             * own readers and never come through here */
            if (nl) *nl = '\0';
            madeira_cfg__trim(buf);
            if (cap) { strncpy(out, buf, cap - 1); out[cap - 1] = '\0'; }
            found = 1;
        }
    }
    free(buf);
    return found;
}

/* Convenience: the value as an integer, or `dflt` when unset or not a number. */
static long long madeira_cfg_int(const char *key, long long dflt)
{
    char v[64], *end;
    long long r;
    if (!madeira_cfg_get(key, v, sizeof v) || !v[0]) return dflt;
    r = strtoll(v, &end, 0);
    return end == v ? dflt : r;
}

/* 1 when the key is set to "1"/"on"/"true"/"yes", 0 when set to anything else,
 * `dflt` when unset. */
static int madeira_cfg_bool(const char *key, int dflt)
{
    char v[32];
    if (!madeira_cfg_get(key, v, sizeof v)) return dflt;
    return (!strcmp(v, "1") || !strcmp(v, "on") || !strcmp(v, "true") || !strcmp(v, "yes")) ? 1 : 0;
}

#endif /* MADEIRA_CFG_H */
