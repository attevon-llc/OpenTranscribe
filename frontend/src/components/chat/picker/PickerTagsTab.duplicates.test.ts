import { describe, it, expect, vi } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());
const axios = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: axios }));

import PickerTagsTab from './PickerTagsTab.svelte';

describe('PickerTagsTab with the same tag name under two kinds', () => {
  it('renders a chip for each row', async () => {
    axios.get.mockResolvedValue({
      data: [
        { name: 'budget', count: 2 },
        { name: 'budget', count: 5 },
      ],
    });
    const { findAllByTestId } = render(PickerTagsTab, { props: { selected: [] } });
    await waitFor(async () => expect(await findAllByTestId('picker-tag-chip')).toHaveLength(2));
  });
});
