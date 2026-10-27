/**
 * The chat settings tabs follow the WAI-ARIA tabs pattern (#1210): the strip is the shared
 * `ui/Tabs`, every tab controls a real `tabpanel` named by it, arrow keys move between tabs,
 * and the advanced tab can be deep-linked.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));
// The two forms have their own suites; this file is about the tab shell around them.
vi.mock('$components/settings/ChatSettings.svelte', async () => ({
  default: (await import('../../test-mocks/EmptyComponent.svelte')).default,
}));
vi.mock('$components/settings/ChatAdminSettings.svelte', async () => ({
  default: (await import('../../test-mocks/EmptyComponent.svelte')).default,
}));

import ChatSettingsPanel from './ChatSettingsPanel.svelte';

describe('ChatSettingsPanel tabs', () => {
  it('moves from General to Advanced on ArrowRight and shows the Advanced tabpanel', async () => {
    render(ChatSettingsPanel, { props: { isAdmin: true } });
    const general = screen.getByRole('tab', { name: 'chat.settings.tabs.general' });
    expect(general).toHaveAttribute('aria-selected', 'true');
    expect(general).toHaveAttribute('tabindex', '0');

    await fireEvent.keyDown(general, { key: 'ArrowRight' });

    const advanced = screen.getByRole('tab', { name: 'chat.settings.tabs.advanced' });
    expect(advanced).toHaveAttribute('aria-selected', 'true');
    expect(advanced).toHaveAttribute('tabindex', '0');
    expect(general).toHaveAttribute('tabindex', '-1');
    const panel = screen.getByRole('tabpanel', { name: 'chat.settings.tabs.advanced' });
    expect(panel).toHaveAttribute('id', 'tabpanel-chat-advanced');
    expect(panel).toHaveTextContent('chat.settings.tabs.advancedIntro');
  });

  it('gives every tab a panel that exists, so aria-controls never dangles', () => {
    const { container } = render(ChatSettingsPanel, { props: { isAdmin: true } });
    for (const tab of screen.getAllByRole('tab')) {
      const id = tab.getAttribute('aria-controls');
      expect(id).toMatch(/^tabpanel-chat-/);
      expect(container.querySelector(`#${id}`)).not.toBeNull();
    }
    expect(container.querySelector('#tabpanel-chat-general')).toBeVisible();
    expect(container.querySelector('#tabpanel-chat-advanced')).not.toBeVisible();
  });

  it('opens on the Advanced tab for the chat-advanced deep link', () => {
    render(ChatSettingsPanel, { props: { isAdmin: true, initialTab: 'chat-advanced' } });
    expect(screen.getByRole('tab', { name: 'chat.settings.tabs.advanced' })).toHaveAttribute(
      'aria-selected',
      'true'
    );
    expect(screen.getByRole('tabpanel')).toHaveTextContent('chat.settings.tabs.advancedIntro');
  });

  it('gives a non-admin no tab strip and never the Advanced panel, even when deep-linked', () => {
    const { container } = render(ChatSettingsPanel, {
      props: { isAdmin: false, initialTab: 'chat-advanced' },
    });
    expect(screen.queryByRole('tablist')).toBeNull();
    expect(screen.queryByRole('tab')).toBeNull();
    expect(container.querySelector('#tabpanel-chat-advanced')).toBeNull();
    expect(container.querySelector('#tabpanel-chat-general')).toBeVisible();
  });
});
