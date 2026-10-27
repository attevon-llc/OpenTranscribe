/**
 * The privacy pieces in a right-to-left UI (`ar`; `stores/locale.ts` sets
 * `document.documentElement.dir`). jsdom applies no layout and does not load component CSS, so
 * the mirroring contract is checked the way `styles/formFields.test.ts` checks CSS: against the
 * source. Physical `left`/`right` offsets, margins, paddings and alignment would not flip; the
 * logical forms do. A render under `dir="rtl"` proves the controls themselves still work.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { render, screen, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  const t = (key: string, vars?: Record<string, unknown>) =>
    Object.entries(vars ?? {}).reduce(
      (text, [name, value]) => text.replace(`{{${name}}}`, String(value)),
      en[key] ?? key
    );
  return { t: readable(t), locale: readable('ar') };
});
vi.mock('$lib/api/privacy', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/api/privacy')>();
  return { ...actual, PrivacyApi: { getPurgeStatus: vi.fn(), getEffective: vi.fn() } };
});

import PrivacySwitchCard from './PrivacySwitchCard.svelte';
import PurgeConfirmDialog from './PurgeConfirmDialog.svelte';

const COMPONENTS = ['PrivacySwitchCard', 'PurgeConfirmDialog', 'SpeakerPrivacyNotice'];

const PHYSICAL =
  /(?:margin|padding|border)-(?:left|right)\s*:|(?<![-\w])(?:left|right)\s*:|text-align\s*:\s*(?:left|right)|float\s*:|border-(?:top|bottom)-(?:left|right)-radius/;

const styleOf = (name: string) => {
  const source = readFileSync(join(__dirname, `${name}.svelte`), 'utf8');
  return source.match(/<style[^>]*>([\s\S]*?)<\/style>/)?.[1] ?? '';
};

afterEach(() => {
  document.documentElement.removeAttribute('dir');
});

describe.each(COMPONENTS)('%s styles', (name) => {
  it('declare no physical left/right offsets, spacing, borders or alignment', () => {
    const css = styleOf(name);
    expect(css.length).toBeGreaterThan(100);
    expect(css.match(PHYSICAL)).toBeNull();
  });
});

describe('physical-property detector', () => {
  it.each([
    'margin-left: 1px',
    'padding-right: 2px',
    'left: 0',
    'right:0',
    'text-align: left',
    'border-left: 3px solid red',
  ])('flags %s', (declaration) => {
    expect(`.x { ${declaration}; }`).toMatch(PHYSICAL);
  });

  it.each(['inset-inline-start: 3px', 'padding-inline-start: 1rem', 'text-align: start'])(
    'accepts %s',
    (declaration) => {
      expect(`.x { ${declaration}; }`).not.toMatch(PHYSICAL);
    }
  );

  it('sees the logical properties the switch relies on to mirror its knob', () => {
    expect(styleOf('PrivacySwitchCard')).toContain('inset-inline-start');
    expect(styleOf('PurgeConfirmDialog')).toContain('padding-inline-start');
    expect(styleOf('SpeakerPrivacyNotice')).toContain('border-inline-start');
  });
});

describe('under dir="rtl"', () => {
  it('PrivacySwitchCard still renders a working, labelled switch', async () => {
    document.documentElement.setAttribute('dir', 'rtl');
    const onToggle = vi.fn();
    render(PrivacySwitchCard, {
      id: 'voice',
      title: 'التعرف على المتحدثين',
      description: 'مطابقة الأصوات بأسماء المتحدثين.',
      checked: false,
      locked: false,
      onToggle,
      purgeLabel: 'حذف البيانات المخزنة',
      canPurge: true,
    });
    const sw = screen.getByRole('switch', { name: 'التعرف على المتحدثين' });
    expect(sw).not.toBeChecked();
    expect(screen.getByText('مطابقة الأصوات بأسماء المتحدثين.')).toBeInTheDocument();
    expect(screen.getByTestId('voice-purge')).toHaveTextContent('حذف البيانات المخزنة');
    await fireEvent.click(sw);
    expect(onToggle).toHaveBeenCalledExactlyOnceWith(true);
    expect(document.documentElement.dir).toBe('rtl');
  });

  it('PurgeConfirmDialog still renders its labels and keeps the typed word left-to-right', async () => {
    document.documentElement.setAttribute('dir', 'rtl');
    render(PurgeConfirmDialog, {
      isOpen: true,
      onClose: vi.fn(),
      targets: ['voiceprints'],
      scopeLabel: 'مؤسستك',
      start: vi.fn().mockResolvedValue({ task_id: 't-1' }),
      pollMs: 5,
    });
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByTestId('purge-confirm')).toBeDisabled();
    expect(screen.getByRole('checkbox')).toBeInTheDocument();
    // "PURGE" is a Latin literal the API compares byte for byte; it must not reorder in RTL.
    expect(screen.getByRole('textbox')).toHaveAttribute('dir', 'ltr');

    await fireEvent.click(screen.getByRole('checkbox'));
    await fireEvent.input(screen.getByRole('textbox'), { target: { value: 'PURGE' } });
    expect(screen.getByTestId('purge-confirm')).toBeEnabled();
  });
});
