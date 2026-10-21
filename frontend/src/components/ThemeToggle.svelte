<script>
  import { theme, toggleTheme } from '../stores/theme';
  import { t } from '$stores/locale';

  // Determine whether to show the toggle as checked (dark mode)
  $: isDarkMode = $theme === 'dark';
</script>

<label class="theme-toggle" title={$t('theme.toggle')}>
  <!-- The wrapping <label> is an implicit label, but it contains only SVG icons and no text,
       so it supplies no accessible name and axe's `label` rule fires. A screen reader
       announced this as a bare "checkbox". `title` on the label is not a substitute — it is
       not an accessible name for the INPUT. Caught by #972's fixture page, which renders the
       app chrome on a surface where nothing else was competing for the finding. -->
  <input
    type="checkbox"
    aria-label={$t('theme.toggle')}
    checked={isDarkMode}
    on:change={toggleTheme}
  />
  <div class="toggle-track">
    <!-- Sun icon (visible in light mode) -->
    <div class="icon sun-icon">
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="16" height="16" fill="none">
        <!-- Sun center circle -->
        <circle cx="12" cy="12" r="4" fill="#FFD700" stroke="#FFD700" stroke-width="2"/>

        <!-- Perfectly symmetrical rays -->
        <!-- Vertical rays -->
        <line x1="12" y1="2" x2="12" y2="4" stroke="#FFD700" stroke-width="2" stroke-linecap="round"/>
        <line x1="12" y1="20" x2="12" y2="22" stroke="#FFD700" stroke-width="2" stroke-linecap="round"/>

        <!-- Horizontal rays -->
        <line x1="2" y1="12" x2="4" y2="12" stroke="#FFD700" stroke-width="2" stroke-linecap="round"/>
        <line x1="20" y1="12" x2="22" y2="12" stroke="#FFD700" stroke-width="2" stroke-linecap="round"/>

        <!-- Diagonal rays - top left to bottom right -->
        <line x1="6.34" y1="6.34" x2="4.93" y2="4.93" stroke="#FFD700" stroke-width="2" stroke-linecap="round"/>
        <line x1="19.07" y1="19.07" x2="17.66" y2="17.66" stroke="#FFD700" stroke-width="2" stroke-linecap="round"/>

        <!-- Diagonal rays - top right to bottom left -->
        <line x1="6.34" y1="17.66" x2="4.93" y2="19.07" stroke="#FFD700" stroke-width="2" stroke-linecap="round"/>
        <line x1="19.07" y1="4.93" x2="17.66" y2="6.34" stroke="#FFD700" stroke-width="2" stroke-linecap="round"/>
      </svg>
    </div>

    <!-- Moon icon (visible in dark mode) -->
    <div class="icon moon-icon">
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="16" height="16" fill="none">
        <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"
          fill="#4B5563" stroke="#4B5563" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </div>

    <!-- Slider circle -->
    <div class="toggle-thumb"></div>
  </div>
</label>

<style>
  /* Toggle container */
  .theme-toggle {
    position: relative;
    display: inline-block;
    width: 56px;
    height: 28px;
    margin: 0 8px;
  }

  /* Hide the input */
  .theme-toggle input {
    opacity: 0;
    width: 0;
    height: 0;
    position: absolute;
  }

  /* Toggle track background */
  .toggle-track {
    position: relative;
    cursor: pointer;
    width: 100%;
    height: 100%;
    background-color: #f1f5f9; /* Light mode track */
    border-radius: 34px;
    transition: all 0.3s ease;
    /* Ensure strong outline is visible in both modes */
    border: 2px solid #cbd5e1;
    box-shadow: 0 0 3px rgba(0, 0, 0, 0.1);
    display: flex;
    align-items: center;
  }

  /* The checked state for the track */
  input:checked + .toggle-track {
    background-color: #1e293b; /* Dark mode track */
    border-color: #64748b; /* Visible border in dark mode */
    box-shadow: 0 0 4px rgba(255, 255, 255, 0.2);
  }

  /* Icons container and positioning */
  .icon {
    position: absolute;
    top: 50%;
    transform: translateY(-50%);
    width: 18px;
    height: 18px;
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1;
    pointer-events: none; /* Ensure clicks pass through to the track */
  }

  /* Position sun on left side */
  .sun-icon {
    left: 7px;
    opacity: 1;
    transition: opacity 0.3s ease;
  }

  /* Position moon on right side */
  .moon-icon {
    right: 7px;
    opacity: 1;
    transition: opacity 0.3s ease;
  }

  /* Toggle thumb (the sliding circle) */
  .toggle-thumb {
    position: absolute;
    top: 50%;
    left: 3px;
    width: 22px;
    height: 22px;
    border-radius: 50%;
    background-color: white;
    transition: transform 0.3s ease;
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.3);
    z-index: 2;
    transform: translateY(-50%); /* Perfect vertical centering */
  }

  /* Move the thumb when checked */
  input:checked + .toggle-track .toggle-thumb {
    transform: translateX(26px) translateY(-50%); /* Maintain vertical centering while moving horizontally */
  }
</style>
