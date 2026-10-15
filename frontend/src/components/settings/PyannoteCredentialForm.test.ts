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
  getPyannoteCredential: vi.fn(),
  savePyannoteCredential: vi.fn(),
  deletePyannoteCredential: vi.fn(),
  testPyannoteCredential: vi.fn(),
}));
vi.mock('$lib/api/pyannoteCredential', () => api);

import PyannoteCredentialForm from './PyannoteCredentialForm.svelte';

const P = 'settings.speakerIdentification.pyannoteKey';
const status = (over: Record<string, unknown> = {}) => ({
  configured: false,
  locked: false,
  test_status: null,
  test_message: null,
  last_tested: null,
  updated_at: null,
  ...over,
});

beforeEach(() => {
  vi.clearAllMocks();
  api.getPyannoteCredential.mockResolvedValue(status());
});

async function renderForm(props: Record<string, unknown> = {}) {
  const result = render(PyannoteCredentialForm, { props });
  await waitFor(() => expect(result.container.querySelector('#pyannote-api-key')).not.toBeNull());
  const input = result.container.querySelector('#pyannote-api-key') as HTMLInputElement;
  const buttons = () => Array.from(result.container.querySelectorAll('button'));
  const button = (label: string) =>
    buttons().find((b) => b.textContent?.includes(label)) as HTMLButtonElement | undefined;
  return { ...result, input, button };
}
const badge = (c: HTMLElement) =>
  c.querySelector('[data-testid="pyannote-badge"]')?.textContent?.trim();

describe('PyannoteCredentialForm input', () => {
  it('is a password field that disables autofill and is never pre-filled', async () => {
    api.getPyannoteCredential.mockResolvedValue(status({ configured: true }));
    const { input } = await renderForm();
    expect(input.type).toBe('password');
    expect(input.getAttribute('autocomplete')).toBe('off');
    expect(input.value).toBe('');
  });

  it('clears the key after a successful save', async () => {
    api.savePyannoteCredential.mockResolvedValue(status({ configured: true }));
    const { input, button } = await renderForm();
    await fireEvent.input(input, { target: { value: '  sk-secret-key-123  ' } });
    await fireEvent.click(button(`${P}.save`)!);

    await waitFor(() => expect(input.value).toBe(''));
    expect(api.savePyannoteCredential).toHaveBeenCalledWith('sk-secret-key-123');
  });
});

describe('PyannoteCredentialForm states', () => {
  it('not configured: offers Save (disabled until a key is typed), no Remove, test disabled', async () => {
    const { container, input, button } = await renderForm();
    expect(badge(container)).toBe(`${P}.statusNotConfigured`);
    expect(button(`${P}.save`)).toBeDisabled();
    expect(button(`${P}.delete`)).toBeUndefined();
    expect(button(`${P}.test`)).toBeDisabled();

    await fireEvent.input(input, { target: { value: 'abcdefghij' } });
    expect(button(`${P}.save`)).toBeEnabled();
    expect(button(`${P}.test`)).toBeEnabled();
  });

  it('configured: labels the button Replace, offers Remove, and tests the saved key', async () => {
    api.getPyannoteCredential.mockResolvedValue(status({ configured: true }));
    const { container, button } = await renderForm();
    expect(badge(container)).toBe(`${P}.statusConfigured`);
    expect(button(`${P}.replace`)).toBeDisabled();
    expect(button(`${P}.delete`)).toBeEnabled();
    expect(button(`${P}.test`)).toBeEnabled();
  });

  it('shows the verified and failed badges from the stored test result', async () => {
    api.getPyannoteCredential.mockResolvedValue(
      status({ configured: true, test_status: 'success' })
    );
    const a = await renderForm();
    expect(badge(a.container)).toBe(`${P}.statusVerified`);
    a.unmount();

    api.getPyannoteCredential.mockResolvedValue(
      status({ configured: true, test_status: 'failed' })
    );
    const b = await renderForm();
    expect(badge(b.container)).toBe(`${P}.statusFailed`);
  });

  it('locked by the deployment: input and Save disabled, hint shown, Remove still allowed', async () => {
    api.getPyannoteCredential.mockResolvedValue(status({ configured: true, locked: true }));
    const { container, input, button } = await renderForm();
    expect(badge(container)).toBe(`${P}.statusLocked`);
    expect(input).toBeDisabled();
    expect(button(`${P}.replace`)).toBeDisabled();
    expect(button(`${P}.test`)).toBeDisabled();
    expect(button(`${P}.delete`)).toBeEnabled();
    expect(container.querySelector('[data-testid="pyannote-warning"]')?.textContent).toContain(
      `${P}.lockedHint`
    );
  });

  it('warns when the pyannote.ai source is chosen but no key is saved', async () => {
    const { container } = await renderForm({ diarizationSource: 'pyannote' });
    expect(container.querySelector('[data-testid="pyannote-warning"]')?.textContent).toContain(
      `${P}.missingWarning`
    );
  });

  it('does not warn for another source, or when a key exists', async () => {
    const a = await renderForm({ diarizationSource: 'provider' });
    expect(a.container.querySelector('[data-testid="pyannote-warning"]')).toBeNull();
    a.unmount();
    api.getPyannoteCredential.mockResolvedValue(status({ configured: true }));
    const b = await renderForm({ diarizationSource: 'pyannote' });
    expect(b.container.querySelector('[data-testid="pyannote-warning"]')).toBeNull();
  });

  it('disables every control from the parent while it is busy', async () => {
    api.getPyannoteCredential.mockResolvedValue(status({ configured: true }));
    const { input, button } = await renderForm({ disabled: true });
    expect(input).toBeDisabled();
    expect(button(`${P}.test`)).toBeDisabled();
    expect(button(`${P}.delete`)).toBeDisabled();
  });

  it('fails soft when the status cannot be loaded: nothing can be saved', async () => {
    api.getPyannoteCredential.mockRejectedValue(new Error('boom'));
    const { input, button } = await renderForm();
    expect(input).toBeDisabled();
    expect(button(`${P}.save`)).toBeDisabled();
  });
});

describe('PyannoteCredentialForm actions', () => {
  it('maps a 422 on save to the invalid-format message and keeps the typed key', async () => {
    api.savePyannoteCredential.mockRejectedValue({ response: { status: 422 } });
    const { input, button } = await renderForm();
    await fireEvent.input(input, { target: { value: 'abcdefghij' } });
    await fireEvent.click(button(`${P}.save`)!);

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(`${P}.invalidFormat`));
    expect(input.value).toBe('abcdefghij');
  });

  it('tests an unsaved typed key without saving it', async () => {
    api.testPyannoteCredential.mockResolvedValue({
      success: true,
      code: 'connected',
      message: 'Connected',
      response_time_ms: 5,
    });
    const { container, input, button, component } = await renderForm();
    void component;
    await fireEvent.input(input, { target: { value: 'abcdefghij' } });
    await fireEvent.click(button(`${P}.test`)!);

    await waitFor(() =>
      expect(
        container.querySelector('[data-testid="pyannote-test-result"]')?.textContent
      ).toContain(`${P}.testSuccess`)
    );
    expect(api.testPyannoteCredential).toHaveBeenCalledWith('abcdefghij');
    expect(api.savePyannoteCredential).not.toHaveBeenCalled();
  });

  it('tests the saved key when the box is empty, then refreshes the stored result', async () => {
    api.getPyannoteCredential
      .mockResolvedValueOnce(status({ configured: true }))
      .mockResolvedValue(status({ configured: true, test_status: 'failed' }));
    api.testPyannoteCredential.mockResolvedValue({
      success: false,
      code: 'rejected',
      message: 'Key rejected',
      response_time_ms: 9,
    });
    const { container, button } = await renderForm();
    await fireEvent.click(button(`${P}.test`)!);

    await waitFor(() => expect(badge(container)).toBe(`${P}.statusFailed`));
    expect(api.testPyannoteCredential).toHaveBeenCalledWith(undefined);
    expect(container.querySelector('[data-testid="pyannote-test-result"]')?.textContent).toContain(
      `${P}.testRejected`
    );
  });

  it('removes the key only after confirming, and reports whether the source was reverted', async () => {
    api.getPyannoteCredential
      .mockResolvedValueOnce(status({ configured: true }))
      .mockResolvedValue(status());
    api.deletePyannoteCredential.mockResolvedValue({
      deleted: true,
      diarization_source: 'provider',
      source_reverted: true,
    });
    const { container, button } = await renderForm({ diarizationSource: 'pyannote' });
    await fireEvent.click(button(`${P}.delete`)!);
    expect(api.deletePyannoteCredential).not.toHaveBeenCalled();

    const confirm = await waitFor(() => {
      const el = document.querySelector('.modal-delete-button');
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });
    await fireEvent.click(confirm);

    await waitFor(() => expect(api.deletePyannoteCredential).toHaveBeenCalledOnce());
    await waitFor(() => expect(badge(container)).toBe(`${P}.statusNotConfigured`));
    expect(toast.success).toHaveBeenCalledWith(`${P}.deletedReverted`);
  });
});
