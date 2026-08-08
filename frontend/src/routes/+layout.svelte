<script lang="ts">
  import { onMount } from "svelte";
  import { goto, afterNavigate } from "$app/navigation";
  import { page } from "$app/stores";
  import { get } from 'svelte/store';

  // Import theme styles
  import "../styles/theme.css";
  import "../styles/form-elements.css";
  import "../styles/tables.css";
  import "../styles/animations.css";
  import "../styles/search.css";

  // Import auth store
  import { authStore, isAuthenticated, initAuth, authReady, getAuthMethods, accountLifecycle, installAccountLifecycleInterceptor } from "$stores/auth";
  import { loadCapabilities } from "$stores/capabilities";
  import { isCloudEdition } from "$lib/edition";
  import { theme } from "../stores/theme";
  import { locale } from "../stores/locale";
  import { llmStatusStore } from "../stores/llmStatus";
  import { networkStore } from "../stores/network";
  import { unregisterServiceWorkers } from "$lib/serviceWorkerCleanup";
  import { resetScrollLock } from '$lib/scrollLock';
  import { initMonitoring } from '$lib/monitoring';

  // Import components
  import Navbar from "../components/Navbar.svelte";
  import NotificationsPanel from "../components/NotificationsPanel.svelte";
  import ToastContainer from "../components/ToastContainer.svelte";
  import UploadManager from "../components/UploadManager.svelte";
  import AppContent from "../components/AppContent.svelte";
  import SettingsModal from "../components/SettingsModal.svelte";
  import FirstRunWizard from "../components/FirstRunWizard.svelte";
  import ClassificationBanner from "$lib/components/ClassificationBanner.svelte";
  import ConnectionStatusBanner from "$components/ui/ConnectionStatusBanner.svelte";
  import QuotaExceededModal from "$lib/cloud/components/QuotaExceededModal.svelte";

  /**
   * Routes reachable without a session — the ONE definition.
   *
   * This list previously existed twice (an imperative copy in the onMount guard
   * and a `{@const}` copy in the template). They drifted independently, which
   * meant a route could be guarded as public and still render the "redirect in
   * flight" loading screen forever. Add new public routes here only.
   *
   * `/accept-invite` and `/verify-email` are unauthenticated by definition: the
   * account either does not exist yet or cannot sign in until it is verified.
   *
   * Cloud edition: the hosted IdP owns login, sign-up and recovery inline on
   * /login, so the local credential routes are dead there and are redirected.
   */
  const TOKEN_PUBLIC_PATHS = ["/accept-invite", "/verify-email"];
  const LOCAL_CREDENTIAL_PATHS = ["/register", "/forgot-password", "/reset-password"];
  const PUBLIC_PATHS = isCloudEdition
    ? ["/login", ...TOKEN_PUBLIC_PATHS]
    : ["/login", ...LOCAL_CREDENTIAL_PATHS, ...TOKEN_PUBLIC_PATHS];

  // A lifecycle hold (`must_change_password` / expired account) confines the
  // session to the login route, which renders the remedy. See $stores/auth.
  $: lifecycleHold = $accountLifecycle !== null;

  // Classification banner state
  let bannerEnabled = false;
  let bannerClassification: 'UNCLASSIFIED' | 'CUI' | 'FOUO' | 'CONFIDENTIAL' | 'SECRET' | 'TOP SECRET' | 'TOP SECRET//SCI' = 'UNCLASSIFIED';

  /**
   * Handle bfcache (back-forward cache) restoration.
   *
   * When a user logs out and then clicks the back button, browsers may restore
   * the previous page from an in-memory snapshot (bfcache) — bypassing all our
   * store clearing, route guards, and auth checks because the DOM is served
   * from the cached snapshot. This can leak User A's data to User B on shared
   * devices, or briefly flash protected content to logged-out users.
   *
   * The `pageshow` event's `persisted` flag is true when the page was restored
   * from bfcache. When that happens, we force a fresh navigation to re-run the
   * auth check pipeline (initAuth → goto).
   */
  function handlePageShow(event: PageTransitionEvent) {
    if (!event.persisted) return;
    // Page was restored from bfcache — force a hard reload of the current URL
    // so the layout's auth guard re-evaluates with fresh state. Using
    // window.location.reload() bypasses the SPA router entirely, guaranteeing
    // that stale stores/DOM are discarded.
    window.location.reload();
  }

  // Initialize auth state when the component mounts
  onMount(() => {
    window.addEventListener('pageshow', handlePageShow);

    // Observe the two account-lifecycle 403s (object `detail`) on every request.
    installAccountLifecycleInterceptor();

    // Optional, env-gated error reporting — no-op unless VITE_SENTRY_DSN is set.
    void initMonitoring();

    // The app ships no service worker — shed any left over from an older build
    // (and the caches it created) so nobody is pinned to a stale bundle.
    void unregisterServiceWorkers();

    // Initialize theme
    document.documentElement.setAttribute('data-theme', get(theme));

    // Async initialization — use IIFE so we can still return a sync cleanup
    (async () => {
      // Initialize locale/i18n
      await locale.initialize();

      // Initialize network connectivity monitoring
      networkStore.initialize();

      // Edition capabilities (cloud hides platform/self-host surfaces).
      // Fail-open: errors leave community defaults (everything visible).
      void loadCapabilities();

      // Fetch auth methods to get banner settings
      try {
        const authMethods = await getAuthMethods();
        if (authMethods.login_banner_enabled) {
          bannerEnabled = true;
          bannerClassification = (authMethods.login_banner_classification as typeof bannerClassification) || 'UNCLASSIFIED';
        }
      } catch (error) {
        console.warn('[Layout] Failed to fetch auth methods for banner:', error);
      }

      try {
        await initAuth();

        const isAuth = get(isAuthenticated);
        const currentPath = $page.url.pathname;

        // Cloud edition: the hosted IdP owns login/registration/forgot-password
        // (its sign-in component on /login handles sign-up + recovery inline), so
        // the local credential routes are dead — redirect any hit to /login.
        if (isCloudEdition && LOCAL_CREDENTIAL_PATHS.includes(currentPath)) {
          goto("/login", { replaceState: true });
        } else {
          const isPublicPath = PUBLIC_PATHS.includes(currentPath);

          if (!isAuth && !isPublicPath) {
            goto("/login", { replaceState: true });
          } else if (isAuth && isPublicPath) {
            goto("/", { replaceState: true });
          }
        }

        // Initialize LLM status store after authentication is ready
        if (isAuth) {
          try {
            await llmStatusStore.initialize();
          } catch (error) {
            console.warn('[Layout] Failed to initialize LLM status store:', error);
          }
        }
      } catch (error) {
        console.error('Layout: onMount - Error during initAuth or subsequent logic:', error);
        const currentPath = $page.url.pathname;
        if (currentPath !== "/login" && currentPath !== "/register") {
          goto("/login", { replaceState: true });
        }
      }
    })();

    return () => {
      window.removeEventListener('pageshow', handlePageShow);
    };
  });

  // Guarantee no stuck overflow:hidden after any client-side navigation
  afterNavigate(() => {
    resetScrollLock();
  });

  // A lifecycle hold can land on any page (an admin can force a password change
  // mid-session). Bounce to /login, which owns both remedy screens. `goto` keeps
  // this an SPA navigation — a full reload would drop the in-memory reason.
  $: if ($authReady && lifecycleHold && $page.url.pathname !== '/login') {
    goto('/login', { replaceState: true });
  }

</script>

{#if $authReady}
  <!-- PUBLIC_PATHS is defined once, in the script block, and shared with the
       imperative guard above. Do not reintroduce a second copy here. -->
  {@const isPublicPath = PUBLIC_PATHS.includes($page.url.pathname)}

  <!-- Classification Banner (FedRAMP AC-8) - shows on all pages when enabled -->
  {#if bannerEnabled && $isAuthenticated}
    <ClassificationBanner
      classification={bannerClassification}
      position="top"
    />
  {/if}

  <div class="app" class:has-banner={bannerEnabled && $isAuthenticated} style="--banner-offset: {bannerEnabled && $isAuthenticated ? '28px' : '0px'}">
    <ToastContainer />
    {#if $isAuthenticated && !lifecycleHold}
      <Navbar />
      <NotificationsPanel />
      <UploadManager />
      <SettingsModal />
      <FirstRunWizard />
      <ConnectionStatusBanner />
      {#if isCloudEdition}
        <QuotaExceededModal />
      {/if}
    {/if}

    {#if lifecycleHold && isPublicPath}
      <!-- Account-lifecycle hold: the session (if any) can reach nothing but the
           remedy, so render /login bare — chrome would only offer dead links.
           `password_change_required` keeps the session; `account_expired` has
           already torn it down. -->
      <main class="content no-navbar">
        <slot />
      </main>
    {:else if $isAuthenticated && !isPublicPath}
      <!-- Authenticated user on a protected route — render the app -->
      <AppContent>
        <slot />
      </AppContent>
    {:else if !$isAuthenticated && isPublicPath}
      <!-- Unauthenticated user on a public page (login/register/forgot-password) — render it -->
      <main class="content no-navbar">
        <slot />
      </main>
    {:else}
      <!--
        Route mismatch — either:
        - Authenticated user on a public page (will redirect to /) OR
        - Unauthenticated user on a protected page (will redirect to /login)
        Show loading screen while the redirect is in flight to prevent
        Flash of Authenticated Content (FOAC) / protected content leakage.
      -->
      <div class="loading-app">
        <div class="loading-brand">
          <img src="/icons/icon-192x192.png" alt="OpenTranscribe" class="loading-logo" width="64" height="64" />
          <div class="loading-bar"><div class="loading-bar-fill"></div></div>
        </div>
      </div>
    {/if}
  </div>
{:else}
  <!-- Initial auth verification still in progress — block all rendering -->
  <div class="loading-app">
    <div class="loading-brand">
      <img src="/icons/icon-192x192.png" alt="OpenTranscribe" class="loading-logo" width="64" height="64" />
      <div class="loading-bar"><div class="loading-bar-fill"></div></div>
    </div>
  </div>
{/if}

<style>
  .app {
    display: flex;
    flex-direction: column;
    min-height: 100vh;
    min-height: 100dvh;
  }

  /* Offset for classification banner (approx 28px) */
  .app.has-banner {
    padding-top: 28px;
  }

  /* Push navbar down when banner is present */
  :global(.app.has-banner .navbar) {
    top: 28px !important;
  }

  .content {
    flex: 1;
    /* Exposed as a variable so a full-bleed page (chat) can cancel it with a
       negative margin instead of hardcoding — and stay correct across the
       breakpoint below. */
    --content-padding: 1rem;
    padding: var(--content-padding);
    margin-top: var(--content-top, 60px);
  }

  .content.no-navbar {
    margin-top: 0;
  }

  @media (min-width: 768px) {
    .content {
      --content-padding: 2rem;
    }
  }

  .loading-app {
    display: flex;
    justify-content: center;
    align-items: center;
    min-height: 100vh;
    min-height: 100dvh;
    background-color: var(--bg-primary, #f8fafc);
  }

  .loading-brand {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 1.5rem;
  }

  .loading-logo {
    border-radius: 16px;
    animation: loading-pulse 1.8s ease-in-out infinite;
  }

  .loading-bar {
    width: 120px;
    height: 3px;
    background: var(--border-color, #e2e8f0);
    border-radius: 3px;
    overflow: hidden;
  }

  .loading-bar-fill {
    width: 40%;
    height: 100%;
    background: var(--primary-color, #3b82f6);
    border-radius: 3px;
    animation: loading-slide 1.2s ease-in-out infinite;
  }

  @keyframes loading-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.6; }
  }

  @keyframes loading-slide {
    0% { transform: translateX(-100%); }
    100% { transform: translateX(350%); }
  }

  @media (prefers-reduced-motion: reduce) {
    .loading-logo { animation: none; }
    .loading-bar-fill { animation: none; width: 100%; }
  }
</style>
