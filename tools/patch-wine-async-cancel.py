#!/usr/bin/env python3
"""Retain tracked asyncs across cancellation, which can free them synchronously.

NOT OUR FIX. This is the Madeira Build 79 corresponding-source package's
`09-async-cancel-audit/cancel-process-async.patch`, written by Madeira's
developer and applied here verbatim -- including the inline comment it adds,
which still reads "Proposed isolated replacement, not installed" while the
directory's README opens "reviewed and installed". The text is kept as the
developer wrote it rather than tidied; worth resolving with them.

cancel_process_async walks process->asyncs, cancels each match, and relinks
it into a temporary list. It held no reference while doing so. async_terminate
explicitly allows thread_queue_apc to complete an operation synchronously and
drop its last reference, so the async could be freed inside cancel_async and
then read again on the next line -- the heap-use-after-free the developer's
ASan fixture reproduces, and the null-link store at req_cancel_async+0x158 in
the iPad captures.

Three changes, all needed for immediate completion:

  grab_object before cancelling, release after restoring, so a tracked entry
  outlives its own completion;

  attach to the completion group before cancel_async rather than after, so a
  callback that completes the operation sees the membership;

  a construction sentinel (cancel->count = 1, undone by the --count at the
  end) so a synchronous completion cannot drop the group to zero while the
  remaining operations are still being added.

Restoration also re-reads the head of the tracked list each time instead of
LIST_FOR_EACH_ENTRY_SAFE, because destroying one entry can mutate another.

Applied as a CI patch because the wine submodule points at willfaust/wine.
"""
import sys

EDITS = [
    (
        """static int cancel_process_async( struct process *process, struct object *obj, struct thread *thread, client_ptr_t iosb, obj_handle_t *wait_handle )
{
    struct async_cancel *cancel = NULL;
    struct async *async, *next_async;
    struct list tracked;
    int count = 0;

    if (thread && !(cancel = create_async_cancel( process ))) return 0;

    list_init( &tracked );

    /* We can't simply use LIST_FOR_EACH_ENTRY_SAFE here, because currently
     * cancelling an async can cause other asyncs to be removed via
     * async_reselect() */

restart:""",
        """/* Proposed isolated replacement, not installed. Source basis and tests in README.md. */
static int cancel_process_async( struct process *process, struct object *obj, struct thread *thread, client_ptr_t iosb, obj_handle_t *wait_handle )
{
    struct async_cancel *cancel = NULL;
    struct async *async;
    struct list tracked;
    int count = 0;

    if (thread && !(cancel = create_async_cancel( process ))) return 0;
    /* The construction sentinel keeps synchronous completions from destroying
     * this group while the remaining operations are being added. */
    if (cancel) cancel->count = 1;

    list_init( &tracked );

restart:""",
    ),
    (
        """            if (!async->canceled) cancel_async( async );
            if (cancel)
            {
                assert( !async->async_cancel );
                async->async_cancel = cancel;
                cancel->count++;
            }
            list_remove( &async->process_entry );""",
        """            /* Cancellation and reselect may complete this or earlier tracked
             * operations. Retain every tracked entry until it is restored. */
            grab_object( async );
            if (cancel)
            {
                assert( !async->async_cancel );
                async->async_cancel = cancel;
                cancel->count++;
            }
            /* Publish group membership before a callback can complete it. */
            if (!async->canceled) cancel_async( async );
            list_remove( &async->process_entry );""",
    ),
    (
        """    /* Put the asyncs back into the process list */
    LIST_FOR_EACH_ENTRY_SAFE( async, next_async, &tracked, struct async, process_entry )
    {
        list_remove( &async->process_entry );
        list_add_tail( &process->asyncs, &async->process_entry );
    }
    if (cancel)
    {
        if (!cancel->count) release_object( cancel );""",
        """    while (!list_empty( &tracked ))
    {
        async = LIST_ENTRY( list_head( &tracked ), struct async, process_entry );
        list_remove( &async->process_entry );
        list_add_tail( &process->asyncs, &async->process_entry );
        release_object( async );
    }
    if (cancel)
    {
        if (!--cancel->count) release_object( cancel );""",
    ),
]

DONE_MARKER = "The construction sentinel keeps synchronous completions"


def main(path):
    with open(path, encoding="utf-8", errors="surrogateescape") as f:
        s = f.read()
    if DONE_MARKER in s:
        print("::notice::wine async cancellation already retains tracked entries")
        return 0
    for old, new in EDITS:
        n = s.count(old)
        if n != 1:
            print(f"::error::async-cancel anchor not found ({n} matches) in {path} -- pinned wine changed, review needed")
            return 1
        s = s.replace(old, new)
    with open(path, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(s)
    print("::notice::wine async cancellation now retains tracked entries across completion")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
