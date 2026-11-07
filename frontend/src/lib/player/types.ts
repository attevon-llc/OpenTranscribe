/** What the app may ask of a player. Events come from `media` (native HTMLMediaElement events). */
export interface PlayerHandle {
  readonly media: HTMLMediaElement;
  play(): Promise<void>;
  pause(): void;
  /** Seek the media element now (pre-metadata safe, see $lib/utils/mediaReady). */
  seek(time: number): void;
  setPlaybackRate(rate: number): void;
  setCaptionsVisible(visible: boolean): void;
  readonly captionsVisible: boolean;
}
