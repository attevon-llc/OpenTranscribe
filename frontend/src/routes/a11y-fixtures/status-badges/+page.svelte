<script lang="ts">
  import TasksGrid from '$components/fileStatus/TasksGrid.svelte';

  // Issue #972: `TasksGrid.svelte`'s `.status-badge` colour depends only on `task.status`,
  // never on the theme (that's handled entirely by CSS). Scanning the real /file-status page
  // for contrast made the observed violation count a function of which task states
  // happened to be in the dev queue at scan time — the same class of defect that let
  // `file-status::color-contrast` sit allowlisted at `1` while the live count grew to `13`
  // (more failing tasks on screen, same single defect). This route renders one task per
  // status UNCONDITIONALLY, so `backend/tests/e2e/test_a11y.py`'s `file-status-badges`
  // surface always scans all four badge states regardless of fixture data.
  //
  // Reachable only when authenticated (the shared app shell in +layout.svelte gates every
  // route behind `$authReady`/`$isAuthenticated`, same as every other scanned surface), and
  // renders no real ids or backend calls of its own.
  const now = new Date().toISOString();
  const statuses = ['pending', 'in_progress', 'completed', 'failed'] as const;
  const tasks = statuses.map((status, index) => ({
    id: `a11y-fixture-${status}`,
    task_type: index % 2 === 0 ? 'transcription' : 'search_indexing',
    status,
    progress: status === 'in_progress' ? 42 : null,
    error_message: status === 'failed' ? 'Synthetic failure for the a11y badge fixture' : null,
    media_file: {
      uuid: `00000000-0000-0000-0000-00000000000${index}`,
      filename: `a11y-fixture-${status}.wav`,
    },
    created_at: now,
  }));
</script>

<div class="a11y-fixture-page">
  <TasksGrid
    {tasks}
    filteredTasks={tasks}
    tasksLoading={false}
    tasksError={null}
    taskFilter="all"
    taskTypeFilter="all"
    taskPage={1}
    taskTotalPages={0}
  />
</div>

<style>
  .a11y-fixture-page {
    width: 100%;
    padding: 1rem;
  }
</style>
