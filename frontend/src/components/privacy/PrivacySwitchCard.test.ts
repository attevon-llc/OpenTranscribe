/**
 * One privacy feature's switch (issue #1280). It is CONTROLLED: it shows `checked` and only asks
 * the parent to change it, so a refused save leaves it truthful. A layer above the person can
 * lock it, in which case it must say who locked it and must not move, and the purge button is
 * enabled only from the parent's `canPurge`.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';
import PrivacySwitchCard from './PrivacySwitchCard.svelte';

const base = () => ({
  id: 'voice',
  title: 'Speaker recognition',
  description: 'Match voices to named speakers.',
  checked: true,
  onToggle: vi.fn(),
});

const sw = () => screen.getByRole('switch') as HTMLInputElement;

describe('PrivacySwitchCard switch', () => {
  it('is a real switch named by the card title and described by its description', () => {
    render(PrivacySwitchCard, base());
    expect(screen.getByRole('switch', { name: 'Speaker recognition' })).toBe(sw());
    expect(sw().getAttribute('aria-describedby')).toBe('voice-desc');
    expect(document.getElementById('voice-desc')).toHaveTextContent(
      'Match voices to named speakers.'
    );
  });

  it('reflects the value it is given: on', () => {
    render(PrivacySwitchCard, { ...base(), checked: true });
    expect(sw()).toBeChecked();
    expect(sw()).not.toBeDisabled();
  });

  it('reflects the value it is given: off', () => {
    render(PrivacySwitchCard, { ...base(), checked: false });
    expect(sw()).not.toBeChecked();
  });

  it('asks for the opposite value when clicked, and passes that boolean to onToggle', async () => {
    const props = { ...base(), checked: false };
    render(PrivacySwitchCard, props);
    await fireEvent.click(sw());
    expect(props.onToggle).toHaveBeenCalledExactlyOnceWith(true);
  });

  it('asks to turn OFF when it is on', async () => {
    const props = { ...base(), checked: true };
    render(PrivacySwitchCard, props);
    await fireEvent.click(sw());
    expect(props.onToggle).toHaveBeenCalledExactlyOnceWith(false);
  });

  it('stays where the parent put it until the parent changes the value (a refused save never lies)', async () => {
    const { rerender } = render(PrivacySwitchCard, { ...base(), checked: false });
    await fireEvent.click(sw());
    expect(sw()).not.toBeChecked();

    await rerender({ checked: true });
    expect(sw()).toBeChecked();
  });

  it('is a native, focusable control, so the keyboard (Space) toggles it like a click', () => {
    // jsdom does not turn Space into a click; the browser does, but only for a native checkbox
    // that can take focus. This pins the two properties that make that true.
    render(PrivacySwitchCard, base());
    expect(sw().tagName).toBe('INPUT');
    expect(sw().type).toBe('checkbox');
    expect(sw().tabIndex).toBeGreaterThanOrEqual(0);
    sw().focus();
    expect(document.activeElement).toBe(sw());
  });
});

describe('PrivacySwitchCard when a higher layer locked it', () => {
  const locked = () => ({
    ...base(),
    checked: false,
    locked: true,
    lockedReason: 'Turned off by your organization administrator.',
  });

  it('shows who locked it and ties the sentence to the switch', () => {
    render(PrivacySwitchCard, locked());
    expect(screen.getByTestId('voice-locked')).toHaveTextContent(
      'Turned off by your organization administrator.'
    );
    expect(sw().getAttribute('aria-describedby')).toContain('voice-locked');
  });

  it('disables the switch', () => {
    render(PrivacySwitchCard, locked());
    expect(sw()).toBeDisabled();
  });

  it('cannot be toggled, even by a change event that bypasses the disabled attribute', async () => {
    const props = locked();
    render(PrivacySwitchCard, props);
    await fireEvent.click(sw());
    await fireEvent.change(sw());
    expect(props.onToggle).not.toHaveBeenCalled();
    expect(sw()).not.toBeChecked();
  });

  it('shows no lock sentence while the switch is not locked, even if a reason is supplied', () => {
    render(PrivacySwitchCard, { ...base(), lockedReason: 'Turned off by someone.' });
    expect(screen.queryByTestId('voice-locked')).toBeNull();
    expect(sw().getAttribute('aria-describedby')).not.toContain('voice-locked');
  });
});

describe('PrivacySwitchCard while a save is in flight', () => {
  it('disables the switch and ignores change events', async () => {
    const props = { ...base(), busy: true };
    render(PrivacySwitchCard, props);
    expect(sw()).toBeDisabled();
    await fireEvent.change(sw());
    expect(props.onToggle).not.toHaveBeenCalled();
  });
});

describe('PrivacySwitchCard extras', () => {
  it('shows the extra notice and wires it into the switch description', () => {
    render(PrivacySwitchCard, { ...base(), notice: 'Gender is inferred from voice.' });
    expect(screen.getByTestId('voice-notice')).toHaveTextContent('Gender is inferred from voice.');
    expect(sw().getAttribute('aria-describedby')).toContain('voice-notice');
  });

  it('renders no switch for a card that only offers the purge', () => {
    render(PrivacySwitchCard, { ...base(), showSwitch: false, purgeLabel: 'Delete stored data' });
    expect(screen.queryByRole('switch')).toBeNull();
    expect(screen.getByTestId('voice-purge')).toBeInTheDocument();
  });

  it('renders no purge button without a purge label', () => {
    render(PrivacySwitchCard, base());
    expect(screen.queryByTestId('voice-purge')).toBeNull();
  });

  it('enables the purge button only when the parent says so, and forwards the click', async () => {
    const onPurge = vi.fn();
    const props = {
      ...base(),
      purgeLabel: 'Delete stored data',
      purgeHint: 'Irreversible.',
      onPurge,
    };
    const { rerender } = render(PrivacySwitchCard, { ...props, canPurge: false });
    const button = screen.getByTestId('voice-purge') as HTMLButtonElement;
    // jsdom delivers a synthetic click to a disabled button (browsers do not), so the
    // observable contract here is the `disabled` attribute itself.
    expect(button).toBeDisabled();

    await rerender({ canPurge: true });
    expect(button).not.toBeDisabled();
    await fireEvent.click(button);
    expect(onPurge).toHaveBeenCalledTimes(1);
  });

  it('disables the purge button while busy even if purging is allowed', () => {
    render(PrivacySwitchCard, {
      ...base(),
      purgeLabel: 'Delete stored data',
      canPurge: true,
      busy: true,
    });
    expect(screen.getByTestId('voice-purge')).toBeDisabled();
  });
});
