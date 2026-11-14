import { readdirSync, readFileSync, statSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { TASK_TYPE_LABEL_KEYS, taskTypeLabel } from './taskTypeLabels';
import en from '$lib/i18n/locales/en.json';

const TASKS_DIR = resolve(process.cwd(), '../backend/app/tasks');

function pyFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = resolve(dir, name);
    return statSync(full).isDirectory() ? pyFiles(full) : full.endsWith('.py') ? [full] : [];
  });
}

/** Every task_type literal the backend writes: create_task_record(..., "x"), task_type="x", TASK_TYPE = "x". */
function backendTaskTypes(): Set<string> {
  const found = new Set<string>();
  const patterns = [
    /create_task_record\([^)]*?,\s*"([a-z_]+)"\s*\)/g,
    /\btask_type\s*=\s*"([a-z_]+)"/g,
    /^TASK_TYPE\s*=\s*"([a-z_]+)"/gm,
  ];
  for (const file of pyFiles(TASKS_DIR)) {
    const source = readFileSync(file, 'utf8');
    for (const pattern of patterns)
      for (const m of source.matchAll(pattern)) {
        const [, type] = m;
        if (type) found.add(type);
      }
  }
  return found;
}

describe('task type labels', () => {
  it('finds the backend task types (guards the scan itself)', () => {
    const types = backendTaskTypes();
    expect(types.has('speaker_attribute_detection')).toBe(true);
    expect(types.has('search_indexing')).toBe(true);
    expect(types.size).toBeGreaterThanOrEqual(15);
  });

  it('labels every task type the backend writes with an existing key', () => {
    for (const type of backendTaskTypes()) {
      const key = TASK_TYPE_LABEL_KEYS[type];
      expect(key, `no label for backend task type "${type}"`).toMatch(/^[a-zA-Z.]+$/);
      expect(Object.keys(en), `${type} -> ${key} is not a locale key`).toContain(key);
    }
  });

  it('translates known types and title-cases unknown ones', () => {
    const t = (k: string) => `T(${k})`;
    expect(taskTypeLabel('search_indexing', t)).toBe(
      'T(settings.statistics.taskType.searchIndexing)'
    );
    expect(taskTypeLabel('brand_new_job', t)).toBe('Brand New Job');
  });
});
