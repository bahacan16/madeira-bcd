#!/usr/bin/env python3
"""tools/patch-wine-lock-orphan.py: the orphan-lock reaper's opt-in safe mode.

Applies the patch (after the waiter-read patch, as CI does) to a copy of the
submodule's wine/dlls/ntdll/unix/sync.c, then compiles the patched
ios_orphan_check against stubs and runs it as the monitor would:

  - unset: as before. A held SRW lock with 3 parked waiters and no stamp is
    released in ONE call (strikes 1/3, 2/3, 3/3 at once): the fault seen in
    Horizon Zero Dawn on builds 475 and 479;
  - stamped: a lock nobody ever stamped is never released; a lock a live
    thread stamped is released only on the 3rd consecutive call without a
    stamp; a release in between clears the suspicion;
  - 0: nothing is ever released.
"""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[2]
sync = root / 'wine/dlls/ntdll/unix/sync.c'
sys.path.insert(0, str(root / 'tools'))
import importlib.util


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), root / 'tools' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


orphan = load('patch-wine-lock-orphan')
waiter = load('patch-wine-waiter-read')
original = sync.read_text()
patched = orphan.patch(waiter.patch(original))
assert orphan.patch(patched) == patched, 'idempotent'
assert orphan.patch(original).count(orphan.MARKER) == 2, 'applies without the waiter patch too'
assert waiter.patch(orphan.patch(original)) == patched, 'either order gives the same file'
print('PASS: the patch applies to the submodule (with or without the waiter patch, either order) and is idempotent')


def cut(text, start, end):
    i = text.index(start)
    return text[i:text.index(end, i) + len(end)]


waiters = cut(patched, '#define IOS_ALERT_WAITER_MAX 512', '} ios_alert_waiters[IOS_ALERT_WAITER_MAX];')
check = cut(patched, 'void ios_orphan_check( const unsigned long long *live_stamps, int nstamps )', '\n}\n')
assert 'ios_srw_reap_exclusive( lock, 0xDEADull );' in check
legacy = cut(original, 'void ios_orphan_check( const unsigned long long *live_stamps, int nstamps )', '\n}\n')
body_old = legacy.split('{', 1)[1]
for line in body_old.splitlines():
    assert line in check.splitlines(), 'every original line is kept: ' + line

harness = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
typedef uintptr_t ULONG_PTR; typedef unsigned long long ULONGLONG;
#define MS_ASYNC 1
static int msync( void *p, size_t n, int f ) { (void)p; (void)n; (void)f; return 0; }
static int prints;
#define dprintf(fd, ...) (prints++, printf(__VA_ARGS__))
static unsigned long long reaped[16]; static int nreaped;
static void ios_srw_reap_exclusive( unsigned long long lock, unsigned long long teb )
{
    (void)teb; reaped[nreaped++] = lock;
    *(volatile unsigned int *)(uintptr_t)lock = 0x00000008;   /* released, waiters left */
}
''' + waiters + '\n' + check + r'''
#define FAIL(...) do { fprintf(stderr, __VA_ARGS__); exit(1); } while (0)
#include <sys/mman.h>
static unsigned int *word;   /* below 512 GB, as guest locks are (the reaper ignores higher addresses) */

static unsigned long long park( int nwait )
{
    unsigned long long lock = (unsigned long long)(uintptr_t)&word[64];
    int i;
    word[64] = 0x00010001u | (unsigned)(nwait << 1);   /* exclusive owner, nwait parked writers */
    for (i = 0; i < IOS_ALERT_WAITER_MAX; i++) ios_alert_waiters[i].addr = NULL;
    for (i = 0; i < nwait; i++) ios_alert_waiters[i].addr = (void *)(uintptr_t)(lock + 2);
    return lock;
}

int main( int argc, char **argv )
{
    const char *scenario = argv[1];
    unsigned long long none = 0, lock;
    int call;
    void *hint = (void *)(uintptr_t)0x200000000ull;

    word = mmap( hint, 16384, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0 );
    if (word == MAP_FAILED || (uintptr_t)word >= 0x8000000000ull) FAIL( "no low page for the lock (%p)\n", (void *)word );

    if (!strcmp( scenario, "legacy" ))
    {
        lock = park( 3 );
        ios_orphan_check( &none, 0 );
        if (nreaped != 1) FAIL( "legacy: %d releases after one call\n", nreaped );
        printf( "legacy: released after ONE call with 3 waiters (the old fault)\n" );
    }
    else if (!strcmp( scenario, "unstamped" ))
    {
        lock = park( 4 );
        for (call = 0; call < 20; call++) ios_orphan_check( &none, 0 );
        if (nreaped) FAIL( "stamped mode released a lock nobody stamped\n" );
        printf( "stamped: a lock nobody stamped is never released (20 calls)\n" );
    }
    else if (!strcmp( scenario, "orphan" ))
    {
        lock = park( 3 );
        ios_orphan_check( &lock, 1 );                       /* its holder is alive and stamps it */
        if (nreaped) FAIL( "released while stamped\n" );
        for (call = 1; call <= 3; call++)
        {
            ios_orphan_check( &none, 0 );                   /* the holder vanished */
            if (call < 3 && nreaped) FAIL( "released after %d cycles\n", call );
        }
        if (nreaped != 1 || reaped[0] != lock) FAIL( "not released on the 3rd cycle (%d)\n", nreaped );
        printf( "stamped: a stamped lock whose holder vanished is released on the 3rd cycle, not before\n" );
    }
    else if (!strcmp( scenario, "recovers" ))
    {
        lock = park( 3 );
        ios_orphan_check( &lock, 1 );
        ios_orphan_check( &none, 0 );
        ios_orphan_check( &none, 0 );
        word[64] &= ~1u;                                    /* released legitimately */
        ios_orphan_check( &none, 0 );
        word[64] |= 1u;                                     /* taken again */
        ios_orphan_check( &none, 0 );
        ios_orphan_check( &none, 0 );
        if (nreaped) FAIL( "a release between cycles did not clear the suspicion\n" );
        ios_orphan_check( &none, 0 );
        if (nreaped != 1) FAIL( "not released after 3 fresh cycles\n" );
        printf( "stamped: a legitimate release clears the strikes\n" );
    }
    else if (!strcmp( scenario, "off" ))
    {
        lock = park( 5 );
        ios_orphan_check( &lock, 1 );
        for (call = 0; call < 10; call++) ios_orphan_check( &none, 0 );
        if (nreaped) FAIL( "MADEIRA_LOCK_ORPHAN=0 released a lock\n" );
        printf( "off: nothing is released\n" );
    }
    return 0;
}
'''

cc = os.environ.get('CC', 'cc')
if not shutil.which(cc):
    print('SKIP: no C compiler')
    sys.exit(0)
with tempfile.TemporaryDirectory(prefix='madeira-orphan-') as directory:
    c = Path(directory) / 'orphan.c'
    exe = Path(directory) / 'orphan'
    c.write_text(harness)
    subprocess.run([cc, '-std=gnu11', '-Wall', '-Werror', '-Wno-unused-function', str(c), '-o', str(exe)], check=True)
    for scenario, value in (('legacy', None), ('unstamped', 'stamped'), ('orphan', 'stamped'),
                            ('recovers', 'stamped'), ('off', '0')):
        env = dict(os.environ)
        env.pop('MADEIRA_LOCK_ORPHAN', None)
        if value is not None:
            env['MADEIRA_LOCK_ORPHAN'] = value
        subprocess.run([str(exe), scenario], check=True, env=env)
print('PASS: unset keeps the old reaper; stamped releases only a stamped lock after 3 cycles; 0 releases nothing')
