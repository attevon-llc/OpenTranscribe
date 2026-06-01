// Immediate theme initialization to prevent a flash of the wrong theme.
// Externalized from app.html so the production CSP can drop `script-src 'unsafe-inline'`.
// Loaded as a render-blocking <script src> in <head>, so it runs before first paint.
(function () {
  // Get saved theme or use system preference
  const savedTheme = localStorage.getItem('theme');
  let initialTheme;

  if (savedTheme) {
    initialTheme = savedTheme;
  } else {
    // Check for system preference
    initialTheme =
      window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches
        ? 'dark'
        : 'light';
    localStorage.setItem('theme', initialTheme);
  }

  // Apply theme to document immediately - before any rendering happens
  document.documentElement.setAttribute('data-theme', initialTheme);

  // Set theme-aware background color on html and body immediately
  if (initialTheme === 'dark') {
    document.documentElement.style.backgroundColor = '#0f172a';
    document.documentElement.style.color = '#f8fafc';
  } else {
    document.documentElement.style.backgroundColor = '#f8fafc';
    document.documentElement.style.color = '#1e293b';
  }
})();
