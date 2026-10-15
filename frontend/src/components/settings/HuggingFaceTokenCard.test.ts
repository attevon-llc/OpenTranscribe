import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, vars?: unknown) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));
const toast = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
}));
vi.mock('$stores/toast', () => ({ toastStore: toast }));

const api = vi.hoisted(() => ({
  getHfTokenStatus: vi.fn(),
  saveHfToken: vi.fn(),
  removeHfToken: vi.fn(),
  testHfToken: vi.fn(),
}));
vi.mock('$lib/api/hfToken', () => api);

import HuggingFaceTokenCard from './HuggingFaceTokenCard.svelte';

const P = 'settings.engineSettings.hfToken';
const TOKEN = 'hf_' + 'x'.repeat(34);

beforeEach(() => {
  vi.clearAllMocks();
  api.getHfTokenStatus.mockResolvedValue({ is_set: false, source: 'none' });
});

async function renderCard() {
  const result = render(HuggingFaceTokenCard);
  await waitFor(() => expect(result.container.querySelector('#hf-token-input')).not.toBeNull());
  const input = result.container.querySelector('#hf-token-input') as HTMLInputElement;
  const button = (label: string) =>
    Array.from(result.container.querySelectorAll('button')).find(
      (b) => b.textContent?.includes(label)
    ) as HTMLButtonElement | undefined;
  const chip = () =>
    result.container.querySelector('[data-testid="hf-token-status"]')?.textContent?.trim();
  return { ...result, input, button, chip };
}

describe('HuggingFaceTokenCard input', () => {
  it('is a password field with autofill off', async () => {
    const { input } = await renderCard();
    expect(input.type).toBe('password');
    expect(input.getAttribute('autocomplete')).toBe('off');
  });

  it.each([
    ['none', { is_set: false, source: 'none' }],
    ['app', { is_set: true, source: 'app' }],
    ['environment', { is_set: true, source: 'environment' }],
  ])('is never pre-filled (%s)', async (_name, status) => {
    api.getHfTokenStatus.mockResolvedValue(status);
    const { input } = await renderCard();
    expect(input.value).toBe('');
    expect(input.defaultValue).toBe('');
  });

  it('is emptied after a successful save', async () => {
    api.saveHfToken.mockResolvedValue({ is_set: true, source: 'app' });
    const { input, button } = await renderCard();
    await fireEvent.input(input, { target: { value: TOKEN } });
    await fireEvent.click(button(`${P}.save`)!);
    await waitFor(() => expect(api.saveHfToken).toHaveBeenCalledWith(TOKEN));
    await waitFor(() => expect(input.value).toBe(''));
    expect(toast.success).toHaveBeenCalledWith(`${P}.saved`);
  });

  it('keeps what was typed when the save fails, and names the reason from the status only', async () => {
    api.saveHfToken.mockRejectedValue({ response: { status: 422, data: { detail: TOKEN } } });
    const { input, button, container } = await renderCard();
    await fireEvent.input(input, { target: { value: TOKEN } });
    await fireEvent.click(button(`${P}.save`)!);
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(`${P}.saveInvalid`));
    expect(input.value).toBe(TOKEN);
    expect(container.textContent).not.toContain(TOKEN);
  });

  it('never renders the typed token as text', async () => {
    const { input, container } = await renderCard();
    await fireEvent.input(input, { target: { value: TOKEN } });
    expect(container.textContent).not.toContain(TOKEN);
  });
});

describe('HuggingFaceTokenCard states', () => {
  it('not set: chip says so, Save needs input, no Remove, Test needs input', async () => {
    const { chip, button, input } = await renderCard();
    expect(chip()).toBe(`${P}.statusNone`);
    expect(button(`${P}.save`)!.disabled).toBe(true);
    expect(button(`${P}.remove`)).toBeUndefined();
    expect(button(`${P}.test`)!.disabled).toBe(true);
    await fireEvent.input(input, { target: { value: TOKEN } });
    expect(button(`${P}.save`)!.disabled).toBe(false);
    expect(button(`${P}.test`)!.disabled).toBe(false);
  });

  it('saved in Settings: Replace, Remove and Test are available', async () => {
    api.getHfTokenStatus.mockResolvedValue({ is_set: true, source: 'app' });
    const { chip, button } = await renderCard();
    expect(chip()).toBe(`${P}.statusApp`);
    expect(button(`${P}.replace`)).toBeDefined();
    expect(button(`${P}.remove`)).toBeDefined();
    expect(button(`${P}.test`)!.disabled).toBe(false);
  });

  it('from the server environment: explained, testable, not removable', async () => {
    api.getHfTokenStatus.mockResolvedValue({ is_set: true, source: 'environment' });
    const { chip, button, container } = await renderCard();
    expect(chip()).toBe(`${P}.statusEnvironment`);
    expect(container.querySelector('[data-testid="hf-token-note"]')?.textContent).toContain(
      `${P}.environmentNote`
    );
    expect(button(`${P}.remove`)).toBeUndefined();
    expect(button(`${P}.test`)!.disabled).toBe(false);
  });

  it('shows an alert, not an empty card, when the status cannot be loaded', async () => {
    api.getHfTokenStatus.mockRejectedValue(new Error('boom'));
    const { chip, container } = await renderCard();
    expect(chip()).toBe(`${P}.statusUnknown`);
    expect(container.querySelector('[data-testid="hf-token-load-failed"]')).not.toBeNull();
  });

  it('removing needs confirmation, then clears the chip', async () => {
    api.getHfTokenStatus.mockResolvedValue({ is_set: true, source: 'app' });
    api.removeHfToken.mockResolvedValue({ is_set: false, source: 'none' });
    const { button, chip } = await renderCard();
    await fireEvent.click(button(`${P}.remove`)!);
    expect(api.removeHfToken).not.toHaveBeenCalled();
    const confirm = Array.from(document.querySelectorAll('button')).filter(
      (b) => b.textContent?.includes(`${P}.remove`)
    );
    await fireEvent.click(confirm[confirm.length - 1]);
    await waitFor(() => expect(api.removeHfToken).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(chip()).toBe(`${P}.statusNone`));
  });
});

describe('HuggingFaceTokenCard test action', () => {
  it('tests a typed token without saving it', async () => {
    api.testHfToken.mockResolvedValue({ ok: true, code: 'valid', message: 'The token is valid.' });
    const { input, button, container } = await renderCard();
    await fireEvent.input(input, { target: { value: TOKEN } });
    await fireEvent.click(button(`${P}.test`)!);
    await waitFor(() => expect(api.testHfToken).toHaveBeenCalledWith(TOKEN));
    expect(api.saveHfToken).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(
        container.querySelector('[data-testid="hf-token-test-result"]')?.textContent
      ).toContain(`${P}.testValid`)
    );
  });

  it('tests the token in effect when the box is empty', async () => {
    api.getHfTokenStatus.mockResolvedValue({ is_set: true, source: 'app' });
    api.testHfToken.mockResolvedValue({ ok: false, code: 'invalid', message: 'x' });
    const { button, container } = await renderCard();
    await fireEvent.click(button(`${P}.test`)!);
    await waitFor(() => expect(api.testHfToken).toHaveBeenCalledWith(undefined));
    await waitFor(() =>
      expect(
        container.querySelector('[data-testid="hf-token-test-result"]')?.textContent
      ).toContain(`${P}.testInvalid`)
    );
  });

  it('shows the generic failure copy when the test request itself fails', async () => {
    api.getHfTokenStatus.mockResolvedValue({ is_set: true, source: 'app' });
    api.testHfToken.mockRejectedValue(new Error('network'));
    const { button, container } = await renderCard();
    await fireEvent.click(button(`${P}.test`)!);
    await waitFor(() =>
      expect(
        container.querySelector('[data-testid="hf-token-test-result"]')?.textContent
      ).toContain(`${P}.testFailed`)
    );
  });
});
