/* Compile the pinned allocator itself with the smaller-span build overlay. Windows
 * diagnostic calls are inert on this Linux host. Allocation/page/free paths
 * and the FEX iOS corruption guards are the production code. */
#define _GNU_SOURCE
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <pthread.h>

typedef void *PVOID, *HANDLE;
typedef size_t SIZE_T;
typedef unsigned long ULONG;
#define WINAPI
#define MEM_RESERVE 0x2000
#define MEM_RELEASE 0x8000
#define PAGE_NOACCESS 1
#define MemExtendedParameterAddressRequirements 1
typedef struct { PVOID LowestStartingAddress, HighestEndingAddress; SIZE_T Alignment; } MEM_ADDRESS_REQUIREMENTS;
typedef struct { ULONG Type; PVOID Pointer; } MEM_EXTENDED_PARAMETER;
typedef struct { PVOID lpMaximumApplicationAddress; } SYSTEM_INFO;
static void GetSystemInfo(SYSTEM_INFO *si) { si->lpMaximumApplicationAddress = (void *)0x7fffffffffULL; }
static HANDLE GetCurrentProcess(void) { return (void *)-1; }
static HANDLE GetStdHandle(unsigned long which) { (void)which; return 0; }
static int VirtualFree(void *p, size_t size, unsigned type) { (void)p; (void)size; (void)type; abort(); }
static HANDLE CreateFileA(const char *p, unsigned a, unsigned s, void *sec, unsigned d, unsigned f, void *t)
{ (void)p; (void)a; (void)s; (void)sec; (void)d; (void)f; (void)t; return (void *)-1; }
static int WriteFile(HANDLE h, const void *p, unsigned long n, unsigned long *w, void *overlapped)
{
    (void)h; (void)overlapped;
    if (memmem(p, n, "CORRUPT", 7) || memmem(p, n, "[rpm-poison]", 12)) {
        fwrite(p, 1, n, stderr);
        abort();
    }
    if (w) *w = n;
    return 1;
}
volatile int FEX_AllocWatch_Armed;
void FEX_AllocWatch_Event(const void *p, unsigned event) { (void)p; (void)event; }

#include "rpmalloc.c"

#define MiB ((size_t)1 << 20)
#define GiB ((size_t)1 << 30)
#define UNIT ((size_t)1 << 16)
#define CAPACITY (12 * GiB)
#define UNITS (CAPACITY / UNIT)
static unsigned char busy[UNITS];
static unsigned char *arena;
static size_t mapped_live, mapped_peak;
static pthread_mutex_t map_lock = PTHREAD_MUTEX_INITIALIZER;

/* Wine-style exact aligned, first-fit views in an isolated 12 GiB arena.
 * Mapped views start PROT_NONE; only allocator commits make pages accessible. */
static void *map_view(size_t size, size_t alignment, size_t *offset, size_t *mapped_size)
{
    size_t count = (size + UNIT - 1) / UNIT;
    size_t step = alignment > UNIT ? alignment / UNIT : 1;
    pthread_mutex_lock(&map_lock);
    for (size_t start = 0; start + count <= UNITS; start += step) {
        size_t i = 0;
        while (i < count && !busy[start + i]) ++i;
        if (i < count) continue;
        memset(busy + start, 1, count);
        mapped_live += count * UNIT;
        if (mapped_live > mapped_peak) mapped_peak = mapped_live;
        *offset = 0;
        *mapped_size = count * UNIT;
        void *p = arena + start * UNIT;
        pthread_mutex_unlock(&map_lock);
        return p;
    }
    pthread_mutex_unlock(&map_lock);
    return NULL;
}

static void require_owned_pages(void *p, size_t size)
{
    uintptr_t address = (uintptr_t)p, base = (uintptr_t)arena;
    assert(p && size && address >= base && size <= CAPACITY && address - base <= CAPACITY - size);
    assert(global_config.page_size && address % global_config.page_size == 0);
    assert(size % global_config.page_size == 0);
    size_t first = ((unsigned char *)p - arena) / UNIT;
    size_t last = ((unsigned char *)p + size - 1 - arena) / UNIT;
    for (size_t i = first; i <= last; ++i) assert(busy[i]);
}

static void commit_view(void *p, size_t size)
{
    require_owned_pages(p, size);
    assert(mprotect(p, size, PROT_READ | PROT_WRITE) == 0);
}

static void decommit_view(void *p, size_t size)
{
    require_owned_pages(p, size);
    /* Windows decommit/recommit and fresh reservations yield zeroed pages.
     * Darwin's MADV_DONTNEED can retain the previous span header. Replace
     * only this fixture-owned range with fresh anonymous, inaccessible pages;
     * the arena stays reserved and adjacent live views are untouched. */
    assert(mmap(p, size, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0) == p);
}

static void unmap_view(void *p, size_t offset, size_t size)
{
    assert(offset == 0 && size % UNIT == 0);
    assert((uintptr_t)p >= (uintptr_t)arena && ((uintptr_t)p - (uintptr_t)arena) % UNIT == 0);
    size_t start = ((unsigned char *)p - arena) / UNIT;
    size_t count = size / UNIT;
    assert(start + count <= UNITS);
    pthread_mutex_lock(&map_lock);
    for (size_t i = 0; i < count; ++i) assert(busy[start + i]);
    decommit_view(p, size);
    memset(busy + start, 0, count);
    mapped_live -= size;
    pthread_mutex_unlock(&map_lock);
}

static rpmalloc_interface_t memory_interface = {
    .memory_map = map_view, .memory_commit = commit_view,
    .memory_decommit = decommit_view, .memory_unmap = unmap_view,
};

static void init(unsigned advertised_gib)
{
    /* ASan's high application region on Linux. On macOS let the kernel pick
     * a fresh range and trim its alignment padding. */
#if defined(__linux__) && !defined(RPMALLOC_TEST_PORTABLE_MAP)
    arena = mmap((void *)0x400000000000ULL, CAPACITY, PROT_NONE,
                 MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE, -1, 0);
    assert(arena != MAP_FAILED);
#else
    size_t alignment = 16 * MiB;
    void *raw = mmap(NULL, CAPACITY + alignment, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    assert(raw != MAP_FAILED);
    arena = (void *)(((uintptr_t)raw + alignment - 1) & ~(uintptr_t)(alignment - 1));
    size_t prefix = (uintptr_t)arena - (uintptr_t)raw;
    size_t suffix = alignment - prefix;
    if (prefix) assert(munmap(raw, prefix) == 0);
    if (suffix) assert(munmap(arena + CAPACITY, suffix) == 0);
#endif
    ios_fex_band_base = (uintptr_t)arena;
    ios_fex_band_end = (uintptr_t)arena + advertised_gib * GiB - 1;
#ifndef RPMALLOC_TEST_PAGE_SIZE
#define RPMALLOC_TEST_PAGE_SIZE 4096
#endif
    rpmalloc_config_t cfg = {.page_size = RPMALLOC_TEST_PAGE_SIZE, .disable_decommit = 0, .unmap_on_finalize = 1};
    assert(rpmalloc_initialize_config(&memory_interface, &cfg) == 0);
    assert(global_config.page_size <= UNIT && UNIT % global_config.page_size == 0);
}

static void done(void)
{
    rpmalloc_finalize();
    assert(mapped_live == 0);
    assert(munmap(arena, CAPACITY) == 0);
}

static void stamp(void *p, size_t size, uint64_t value)
{
    assert(p && rpmalloc_usable_size(p) >= size);
    memcpy(p, &value, sizeof(value));
    memcpy((char *)p + size - sizeof(value), &value, sizeof(value));
}

static void check(void *p, size_t size, uint64_t value)
{
    uint64_t a, b;
    memcpy(&a, p, sizeof(a));
    memcpy(&b, (char *)p + size - sizeof(b), sizeof(b));
    assert(a == value && b == value);
}

static void expect_bytes(const void *p, size_t size, unsigned char value)
{
    const unsigned char *bytes = p;
    for (size_t i = 0; i < size; ++i) assert(bytes[i] == value);
}

static void memory_contract(void)
{
    size_t offset, left_size, middle_size, right_size, before = mapped_live;
    unsigned char *left = map_view(UNIT, UNIT, &offset, &left_size);
    unsigned char *middle = map_view(2 * UNIT, UNIT, &offset, &middle_size);
    unsigned char *right = map_view(UNIT, UNIT, &offset, &right_size);
    assert(left && middle && right && middle == left + UNIT && right == middle + 2 * UNIT);
    commit_view(left, left_size);
    commit_view(middle, middle_size);
    commit_view(right, right_size);
    expect_bytes(middle, middle_size, 0);
    memset(left, 0x51, left_size);
    memset(middle, 0xa5, middle_size);
    memset(right, 0x73, right_size);

    /* Partial decommit clears only the requested pages, including when
     * emulating the Apple Silicon runner's 16 KiB allocation-page size. */
    size_t page = global_config.page_size;
    decommit_view(middle + page, page);
    commit_view(middle + page, page);
    expect_bytes(middle, page, 0xa5);
    expect_bytes(middle + page, page, 0);
    expect_bytes(middle + 2 * page, middle_size - 2 * page, 0xa5);

    /* Reusing the same address must not retain allocator metadata or payload. */
    unmap_view(middle, 0, middle_size);
    size_t reused_size;
    unsigned char *reused = map_view(2 * UNIT, UNIT, &offset, &reused_size);
    assert(reused == middle && reused_size == middle_size);
    commit_view(reused, reused_size);
    expect_bytes(reused, reused_size, 0);
    expect_bytes(left, left_size, 0x51);
    expect_bytes(right, right_size, 0x73);
    unmap_view(left, 0, left_size);
    unmap_view(reused, 0, reused_size);
    unmap_view(right, 0, right_size);
    assert(mapped_live == before);
    printf("PASS: fresh/recommitted pages zeroed, same-address reuse, adjacent views preserved (%zu-byte pages)\n", page);
}

static void geometry(unsigned span_mb)
{
    /* Normal thread-owned heaps, as used by FEX. First-class heaps have a
     * separate bulk-free contract for huge blocks. */
    rpmalloc_heap_t *h = heap_allocate(0);
    size_t sizes[] = {48, 20 * 1024, 512 * 1024};
    size_t expected[] = {SPAN_SIZE, SPAN_SIZE, SPAN_SIZE};
    assert(SPAN_SIZE == span_mb * MiB);
    assert(LARGE_PAGE_SIZE == SPAN_SIZE);
    assert(MEDIUM_PAGE_SIZE <= SPAN_SIZE && SPAN_SIZE % MEDIUM_PAGE_SIZE == 0);
    for (unsigned i = 0; i < SIZE_CLASS_COUNT; ++i) assert(global_size_class[i].block_count);
    for (unsigned i = 0; i < 3; ++i) {
        void *p = rpmalloc_heap_alloc(h, sizes[i]);
        stamp(p, sizes[i], 100 + i);
        span_t *span = block_get_span(p);
        assert(((uintptr_t)span & (SPAN_SIZE - 1)) == 0);
        assert(span->mapped_size == expected[i]);
        assert((size_t)span->page_count * span->page_size == expected[i]);
    }
    /* Fill several spans and verify every retained block before freeing it. */
    enum { N = 4000 };
    void **pointers = calloc(N, sizeof(*pointers));
    assert(pointers);
    for (size_t size = 4096; size <= 256 * 1024; size *= 64) {
        for (unsigned i = 0; i < N; ++i) {
            pointers[i] = rpmalloc_heap_alloc(h, size);
            stamp(pointers[i], size, i + size);
        }
        for (unsigned i = N; i-- > 0;) {
            check(pointers[i], size, i + size);
            rpmalloc_heap_free(h, pointers[i]);
        }
    }
    free(pointers);
    size_t boundaries[] = {3 * MiB, 7 * MiB / 2 - 1, 7 * MiB / 2, 7 * MiB / 2 + 1,
                           4 * MiB - 1, 4 * MiB, 4 * MiB + 1, 7 * MiB - 1, 7 * MiB, 7 * MiB + 1,
                           8 * MiB - 1, 8 * MiB, 8 * MiB + 1, 10 * MiB, 16 * MiB, 16 * MiB + 1};
    for (unsigned i = 0; i < sizeof(boundaries) / sizeof(*boundaries); ++i) {
        /* Unaligned requests expose the last size class itself. Adding an
         * alignment first would silently turn an 8 MiB boundary into huge. */
        void *p = rpmalloc_heap_alloc(h, boundaries[i]);
        stamp(p, boundaries[i], i + 200);
        check(p, boundaries[i], i + 200);
        if (span_mb < 16 && boundaries[i] > LARGE_BLOCK_SIZE_LIMIT)
            assert(block_get_span(p)->page_type == PAGE_HUGE);
        rpmalloc_heap_free(h, p);
        for (size_t alignment = 4096; alignment < RPMALLOC_MAX_ALIGNMENT; alignment *= 2) {
            p = rpmalloc_heap_aligned_alloc(h, alignment, boundaries[i]);
            assert(((uintptr_t)p & (alignment - 1)) == 0);
            stamp(p, boundaries[i], i + 300);
            check(p, boundaries[i], i + 300);
            rpmalloc_heap_free(h, p);
        }
    }
    /* Exercise every class and the existing realloc path across the new
     * large/huge boundary; allocator ownership and freeing remain real. */
    for (unsigned i = 1; i < SIZE_CLASS_COUNT; ++i) {
        size_t n = global_size_class[i].block_size;
        void *p = rpmalloc_heap_alloc(h, n);
        stamp(p, n, i + 400);
        check(p, n, i + 400);
        rpmalloc_heap_free(h, p);
    }
    void *p = rpmalloc_heap_alloc(h, 7 * MiB);
    stamp(p, 7 * MiB, 5678);
    p = rpmalloc_heap_realloc(h, p, 10 * MiB, 0);
    assert(p);
    check(p, 7 * MiB, 5678);
    stamp(p, 6 * MiB, 9012);
    p = rpmalloc_heap_realloc(h, p, 6 * MiB, 0);
    check(p, 6 * MiB, 9012);
    rpmalloc_heap_free(h, p);
    errno = 0;
    assert(!rpmalloc_heap_aligned_alloc(h, RPMALLOC_MAX_ALIGNMENT, 4096) && errno == EINVAL);
    rpmalloc_heap_free_all(h);
    rpmalloc_heap_release(h);
}

static void *remote_free(void *p)
{
    rpmalloc_thread_initialize();
    rpfree(p);
    rpmalloc_thread_finalize();
    return NULL;
}

static void cross_thread(void)
{
    void *p = rpmalloc(2048);
    stamp(p, 2048, 123);
    pthread_t t;
    assert(pthread_create(&t, NULL, remote_free, p) == 0);
    assert(pthread_join(t, NULL) == 0);
    /* Exercise the original remote-free adoption path on the owning heap. */
    for (unsigned i = 0; i < 5000; ++i) {
        p = rpmalloc(2048);
        stamp(p, 2048, i);
        check(p, 2048, i);
        rpfree(p);
    }
}

static void pressure(unsigned span_mb)
{
    size_t offset, size;
    void *furniture = map_view(4 * GiB, SPAN_SIZE, &offset, &size);
    assert(furniture);
    rpmalloc_heap_t *heaps[96] = {0};
    void *direct[96][4] = {{0}};
    size_t direct_sizes[96][4] = {{0}};
    unsigned completed = 0;
    for (unsigned i = 0; i < 96; ++i) {
        heaps[i] = heap_allocate(0);
        size_t sizes[] = {48, 20 * 1024, 512 * 1024};
        int okay = 1;
        for (unsigned j = 0; j < 3; ++j) {
            void *p = rpmalloc_heap_alloc(heaps[i], sizes[j]);
            if (!p) { okay = 0; break; }
            stamp(p, sizes[j], i * 3 + j);
        }
        if (!okay) break;
        /* Controlled pressure, with unchanged L1, call/ret, IR and frontend
         * requests. The 64 KiB allocation-unit model rounds small views;
         * this is a regression workload, not a device-log replay. */
        size_t demands[] = {2 * MiB, 16 * MiB + 8192, 16 * MiB, 8 * MiB};
        for (unsigned j = 0; j < 4; ++j) {
            size_t ignored;
            direct[i][j] = map_view(demands[j], UNIT, &ignored, &direct_sizes[i][j]);
            if (!direct[i][j]) { okay = 0; break; }
        }
        if (!okay) break;
        ++completed;
    }
    if (span_mb < 16) {
        fprintf(stderr, "span%u pressure: completed=%u reserved=%zuMiB\n", span_mb, completed, mapped_peak / MiB);
        assert(completed == 96);
        void *p = rpmalloc_heap_alloc(heaps[95], 10 * MiB);
        stamp(p, 10 * MiB, 999);
        check(p, 10 * MiB, 999);
        rpmalloc_heap_free(heaps[95], p);
    } else {
        assert(completed < 96); /* negative control: the old reservation exhausts this budget */
    }
    printf("PASS: pressure %u/96 heaps, %s, peak reserved=%zuMiB\n", completed,
           span_mb < 16 ? "10MiB request succeeds" : "original geometry reaches OOM", mapped_peak / MiB);
    for (unsigned i = 0; i < 96; ++i) if (heaps[i]) {
        rpmalloc_heap_free_all(heaps[i]);
        rpmalloc_heap_release(heaps[i]);
    }
    for (unsigned i = 0; i < 96; ++i) for (unsigned j = 0; j < 4; ++j)
        if (direct[i][j]) unmap_view(direct[i][j], 0, direct_sizes[i][j]);
    unmap_view(furniture, offset, size);
}

static void late_pressure(unsigned span_mb)
{
    /* Many simultaneously retained heaps. This is a controlled workload,
     * not a replay: 194 initial threads with four allocation classes each,
     * extra large-class demand on every fourth heap, 39 IR/frontend pairs
     * and unchanged guarded 16 MiB call/ret and 2 MiB lookup reservations. */
    enum { INITIAL = 194, TARGET = 220 };
    rpmalloc_heap_t *heaps[TARGET] = {0};
    void *blocks[TARGET][5] = {{0}};
    size_t sizes[] = {48, 20 * 1024, 512 * 1024, 3 * MiB, 1536 * 1024};
    void *direct[TARGET][2] = {{0}}, *ir[39] = {0};
    size_t direct_sizes[TARGET][2] = {{0}}, ir_sizes[39] = {0};
    size_t offset, furniture_size;
    void *furniture = map_view(GiB, UNIT, &offset, &furniture_size);
    assert(furniture);
    for (unsigned i = 0; i < 39; ++i) {
        size_t ignored;
        ir[i] = map_view(24 * MiB, UNIT, &ignored, &ir_sizes[i]);
        assert(ir[i]);
    }
    unsigned completed = 0;
    for (unsigned i = 0; i < TARGET; ++i) {
        heaps[i] = heap_allocate(0);
        if (!heaps[i]) break;
        int okay = 1;
        unsigned count = i % 4 ? 4 : 5;
        for (unsigned j = 0; j < count; ++j) {
            blocks[i][j] = rpmalloc_heap_alloc(heaps[i], sizes[j]);
            if (!blocks[i][j]) { okay = 0; break; }
            stamp(blocks[i][j], sizes[j], i * 5 + j);
        }
        if (!okay) break;
        size_t demands[] = {2 * MiB, 16 * MiB + 8192};
        for (unsigned j = 0; j < 2; ++j) {
            size_t ignored;
            direct[i][j] = map_view(demands[j], UNIT, &ignored, &direct_sizes[i][j]);
            if (!direct[i][j]) { okay = 0; break; }
        }
        if (!okay) break;
        ++completed;
    }
    fprintf(stderr, "span%u late pressure: %u/%u heaps (initial=%u), peak reserved=%zuMiB\n",
           span_mb, completed, TARGET, INITIAL, mapped_peak / MiB);
    if (span_mb == 4) assert(completed == TARGET);
    else assert(span_mb == 8 && completed > 0 && completed < TARGET);
    for (unsigned i = 0; i < TARGET; ++i) if (heaps[i]) {
        for (unsigned j = 0; j < 5; ++j)
            if (blocks[i][j]) check(blocks[i][j], sizes[j], i * 5 + j);
        rpmalloc_heap_free_all(heaps[i]);
        rpmalloc_heap_release(heaps[i]);
    }
    for (unsigned i = 0; i < TARGET; ++i) for (unsigned j = 0; j < 2; ++j)
        if (direct[i][j]) unmap_view(direct[i][j], 0, direct_sizes[i][j]);
    for (unsigned i = 0; i < 39; ++i) unmap_view(ir[i], 0, ir_sizes[i]);
    unmap_view(furniture, offset, furniture_size);
}

int main(int argc, char **argv)
{
    assert(argc == 4);
    unsigned gib = (unsigned)strtoul(argv[1], NULL, 10);
    unsigned span_mb = atoi(argv[2]) == 0 ? 16 : atoi(argv[2]) == 1 ? 8 : 4;
    init(gib);
    memory_contract();
    if (!strcmp(argv[3], "pressure")) pressure(span_mb);
    else if (!strcmp(argv[3], "late_pressure")) late_pressure(span_mb);
    else { geometry(span_mb); cross_thread(); puts("PASS: geometry, span boundaries, aligned/huge allocations and remote frees"); }
    done();
    return 0;
}
