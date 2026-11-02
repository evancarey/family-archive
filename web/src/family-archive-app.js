import { LitElement, html } from "lit";
import { beginSignIn, currentAccessToken, endSignIn, finishSignIn } from "./auth.js";
import { hasPresentationConfig } from "./config.js";
import {
  checkLibraryApi,
  fetchLibraryMedia,
  SignInRequiredError,
  updateFileTags,
} from "./library-api.js";
import { PAGE_SIZE_OPTIONS, readBrowseState, writeBrowseState } from "./browse-state.js";
import { pageTokens, placeholderCount } from "./pagination.js";
import { fitImageDimensions, requiresPreviewForViewer, viewerImageSource } from "./image-display.js";
import {
  archiveEntryKey,
  imageRotationFor,
  imageRotationKey,
  rotateAngle,
} from "./rotation.js";
import { mockLibraryEnabled, readMockLibraryPage } from "./mock-library.js";

const ROTATION_STORAGE_KEY = "family-archive:thumbnail-rotations:v1";
const VIEWER_FADE_DURATION_MS = 200;

const loadRotations = () => {
  try {
    const saved = JSON.parse(window.localStorage.getItem(ROTATION_STORAGE_KEY) || "{}");
    if (!saved || typeof saved !== "object" || Array.isArray(saved)) return {};
    return Object.fromEntries(
      Object.entries(saved).filter(([, angle]) => [0, 90, 180, 270].includes(angle)),
    );
  } catch {
    return {};
  }
};

const entryKey = archiveEntryKey;
const rotationKey = imageRotationKey;
const rotationFor = imageRotationFor;
const mediaNoun = (mediaType) => (mediaType === "video" ? "video" : "photograph");
const mediaPlural = (mediaType) => (mediaType === "video" ? "videos" : "photographs");

class FamilyArchiveApp extends LitElement {
  static properties = {
    view: { state: true },
    statusTitle: { state: true },
    statusMessage: { state: true },
    statusState: { state: true },
    showStatus: { state: true },
    items: { state: true },
    sources: { state: true },
    total: { state: true },
    offset: { state: true },
    pageSize: { state: true },
    mediaType: { state: true },
    query: { state: true },
    sourceIds: { state: true },
    libraryMessage: { state: true },
    loading: { state: true },
    layout: { state: true },
    sort: { state: true },
    rotations: { state: true },
    editingRotationKey: { state: true },
    viewerOpen: { state: true },
    selectedPhotoKey: { state: true },
    viewerLoading: { state: true },
    viewerImageVisible: { state: true },
    viewerTransitioning: { state: true },
    viewerMessage: { state: true },
    videoPlaybackErrorKey: { state: true },
    tagDrafts: { state: true },
    savingTagHashes: { state: true },
  };

  constructor() {
    super();
    const browseState = readBrowseState();
    this.view = "preview";
    this.statusTitle = "Local preview";
    this.statusMessage = "The page is available to preview. Sign-in becomes available after AWS settings are configured.";
    this.statusState = "neutral";
    this.showStatus = true;
    this.items = [];
    this.sources = [];
    this.total = 0;
    this.offset = browseState.offset;
    this.pageSize = browseState.pageSize;
    this.mediaType = browseState.mediaType;
    this.query = browseState.query;
    this.sourceIds = browseState.sourceIds;
    this.libraryMessage = "";
    this.loading = false;
    this.layout = browseState.layout;
    this.sort = browseState.sort;
    this.rotations = loadRotations();
    this.editingRotationKey = "";
    this.viewerOpen = false;
    this.selectedPhotoKey = "";
    this.viewerLoading = false;
    this.viewerImageVisible = false;
    this.viewerTransitioning = false;
    this.viewerMessage = "";
    this.videoPlaybackErrorKey = "";
    this.tagDrafts = {};
    this.savingTagHashes = [];
    this.requestId = 0;
    this.searchTimer = null;
    this.mockMode = mockLibraryEnabled();
    this.handleDocumentClick = (event) => {
      const filter = this.querySelector(".source-filter");
      if (filter?.open && !filter.contains(event.target)) filter.open = false;

      const editPanel = this.querySelector(".image-viewer .viewer-edit-panel");
      const editButton = this.querySelector(".image-viewer .viewer-edit-toggle");
      if (
        this.viewerOpen
        && this.editingRotationKey
        && !editPanel?.contains(event.target)
        && !editButton?.contains(event.target)
      ) {
        this.editingRotationKey = "";
      }
    };
  }

  connectedCallback() {
    super.connectedCallback();
    document.addEventListener("click", this.handleDocumentClick);
  }

  disconnectedCallback() {
    document.removeEventListener("click", this.handleDocumentClick);
    super.disconnectedCallback();
  }

  createRenderRoot() {
    return this;
  }

  firstUpdated() {
    this.initialize();
  }

  async initialize() {
    if (this.mockMode) {
      this.view = "library";
      this.showStatus = true;
      this.statusTitle = "Local mock library";
      this.statusMessage = "Sample photos only. No sign-in or AWS requests are used.";
      this.statusState = "ready";
      await this.loadPhotos({ offset: this.offset });
      return;
    }
    if (!hasPresentationConfig()) {
      this.view = "preview";
      return;
    }
    try {
      await finishSignIn();
      if (!currentAccessToken()) {
        this.view = "signed-out";
        this.showStatus = false;
        return;
      }
      this.view = "library";
      this.showStatus = true;
      this.statusTitle = "You’re signed in";
      this.statusMessage = "Checking the library service.";
      this.statusState = "pending";
      const ready = await checkLibraryApi();
      if (!ready) throw new Error("The library service is not ready yet.");
      this.statusMessage = "Your secure connection is ready.";
      this.statusState = "ready";
      await this.updateComplete;
      this.syncRestoredPageSize();
      await this.loadPhotos({ offset: this.offset });
    } catch (error) {
      if (error instanceof SignInRequiredError) {
        this.view = "signed-out";
        this.showStatus = true;
        this.statusTitle = "Sign-in required";
        this.statusMessage = error.message;
        this.statusState = "error";
        return;
      }
      this.view = currentAccessToken() ? "library" : "signed-out";
      this.showStatus = true;
      this.statusTitle = currentAccessToken() ? "We couldn’t load the library" : "Sign-in unavailable";
      this.statusMessage = error.message;
      this.statusState = "error";
    }
  }

  syncRestoredPageSize() {
    const selected = Number(this.querySelector(".page-size-filter select")?.value);
    if (!PAGE_SIZE_OPTIONS.includes(selected) || selected === this.pageSize) return;
    this.pageSize = selected;
    this.saveBrowseState();
  }

  async signIn() {
    try {
      await beginSignIn();
    } catch (error) {
      this.showStatus = true;
      this.statusTitle = "Sign-in unavailable";
      this.statusMessage = error.message;
      this.statusState = "error";
    }
  }

  async signOut() {
    try {
      await endSignIn();
    } catch (error) {
      this.statusTitle = "Sign-out unavailable";
      this.statusMessage = error.message;
      this.statusState = "error";
      this.showStatus = true;
    }
  }

  async loadPhotos({ offset = 0, viewerTarget = "" } = {}) {
    // Keep the current viewer item and its position on screen while the
    // adjacent page is fetched. The requested offset is committed on success.
    if (!viewerTarget) {
      this.offset = offset;
      this.saveBrowseState(offset);
    }
    if (this.mockMode) {
      const page = readMockLibraryPage({
        query: this.query,
        sourceIds: this.sourceIds,
        mediaType: this.mediaType,
        sort: this.sort,
        offset,
        limit: this.pageSize,
      });
      if (page.total > 0 && offset >= page.total) {
        const lastPageOffset = Math.floor((page.total - 1) / this.pageSize) * this.pageSize;
        return this.loadPhotos({ offset: lastPageOffset, viewerTarget });
      }
      this.sources = page.sources;
      this.items = page.items;
      this.total = page.total;
      this.offset = page.total === 0 ? 0 : offset;
      this.loading = false;
      this.libraryMessage = this.mediaType === "video"
        ? "The local preview contains sample photos only."
        : "Local mock data — downloads use sample SVGs, not archive originals.";
      this.saveBrowseState();
      if (viewerTarget && page.items.length) {
        const targetItem = viewerTarget === "first" ? page.items[0] : page.items.at(-1);
        this.selectedPhotoKey = entryKey(targetItem);
        this.viewerLoading = false;
        this.viewerTransitioning = false;
        this.viewerImageVisible = true;
        this.viewerMessage = "";
      }
      return;
    }
    const requestId = ++this.requestId;
    this.loading = true;
    this.libraryMessage = `Loading ${mediaPlural(this.mediaType)}…`;

    try {
      const { status, payload } = await fetchLibraryMedia({
        query: this.query.trim(),
        sourceIds: this.sourceIds,
        offset,
        limit: this.pageSize,
        mediaType: this.mediaType,
        sort: this.sort,
      });
      if (requestId !== this.requestId) return;
      if (status === 404) {
        this.libraryMessage = "Your account is ready. The family library has not been published yet.";
        this.items = [];
        this.sources = [];
        this.total = 0;
        this.offset = 0;
        this.saveBrowseState();
        this.viewerLoading = false;
        this.viewerTransitioning = false;
        this.viewerImageVisible = true;
        this.loading = false;
        return;
      }
      if (payload.total > 0 && offset >= payload.total) {
        const lastPageOffset = Math.floor((payload.total - 1) / this.pageSize) * this.pageSize;
        this.loadPhotos({ offset: lastPageOffset, viewerTarget });
        return;
      }
      if (viewerTarget && payload.items.length === 0) {
        this.viewerLoading = false;
        this.viewerTransitioning = false;
        this.viewerImageVisible = true;
        this.viewerMessage = "There are no more photographs in that direction.";
        this.loading = false;
        return;
      }
      this.sources = payload.sources || [];
      this.items = payload.items;
      this.total = payload.total;
      this.offset = payload.total === 0 ? 0 : offset;
      this.loading = false;
      this.saveBrowseState();
      if (viewerTarget) {
        const targetItem = viewerTarget === "first" ? payload.items[0] : payload.items.at(-1);
        this.selectedPhotoKey = entryKey(targetItem);
        this.viewerLoading = false;
        this.viewerTransitioning = false;
        this.viewerImageVisible = !viewerImageSource(targetItem);
        this.viewerMessage = "";
        this.videoPlaybackErrorKey = "";
      }
      this.libraryMessage = payload.items.length
        ? `Select a ${mediaNoun(this.mediaType)} to open it.`
        : `No ${mediaPlural(this.mediaType)} match this search.`;
    } catch (error) {
      if (requestId !== this.requestId) return;
      this.loading = false;
      this.libraryMessage = error.message;
      if (error instanceof SignInRequiredError) {
        this.view = "signed-out";
        this.showStatus = true;
        this.statusTitle = "Sign-in required";
        this.statusMessage = error.message;
        this.statusState = "error";
      }
      if (viewerTarget) {
        this.viewerLoading = false;
        this.viewerTransitioning = false;
        this.viewerImageVisible = true;
        this.viewerMessage = error.message;
      }
    }
  }

  handleSearch(event) {
    this.query = event.currentTarget.value;
    this.offset = 0;
    this.saveBrowseState(0);
    window.clearTimeout(this.searchTimer);
    this.searchTimer = window.setTimeout(() => this.loadPhotos(), 250);
  }

  handleSource(event) {
    const { value, checked } = event.currentTarget;
    const selected = new Set(this.sourceIds);
    if (checked) selected.add(value);
    else selected.delete(value);
    this.sourceIds = [...selected];
    this.items = [];
    this.offset = 0;
    this.saveBrowseState(0);
    this.loadPhotos({ offset: 0 });
  }

  clearSources() {
    this.sourceIds = [];
    this.items = [];
    this.offset = 0;
    this.saveBrowseState(0);
    this.loadPhotos({ offset: 0 });
  }

  selectMediaType(mediaType) {
    if (!["photo", "video"].includes(mediaType) || mediaType === this.mediaType) return;
    this.mediaType = mediaType;
    this.offset = 0;
    this.items = [];
    this.videoPlaybackErrorKey = "";
    this.libraryMessage = `Loading ${mediaPlural(mediaType)}…`;
    this.saveBrowseState(0);
    this.loadPhotos({ offset: 0 });
  }

  handlePageSize(event) {
    this.pageSize = Number(event.currentTarget.value);
    this.loadPhotos({ offset: this.offset });
  }

  setLayout(layout) {
    this.layout = layout;
    this.saveBrowseState();
  }

  handleSort(event) {
    this.sort = event.currentTarget.value;
    this.items = [];
    this.offset = 0;
    this.saveBrowseState(0);
    this.loadPhotos({ offset: 0 });
  }

  saveBrowseState(offset = this.offset) {
    writeBrowseState({
      offset,
      pageSize: this.pageSize,
      query: this.query,
      sourceIds: this.sourceIds,
      mediaType: this.mediaType,
      layout: this.layout,
      sort: this.sort,
    });
  }

  openPhoto(item) {
    this.selectedPhotoKey = entryKey(item);
    this.editingRotationKey = "";
    this.viewerOpen = true;
    this.viewerImageVisible = false;
    this.viewerTransitioning = false;
    this.viewerMessage = "";
    this.videoPlaybackErrorKey = "";
    this.updateComplete.then(() => this.querySelector(".image-viewer")?.focus());
  }

  closeViewer() {
    this.viewerOpen = false;
    this.selectedPhotoKey = "";
    this.viewerImageVisible = false;
    this.viewerTransitioning = false;
    this.viewerMessage = "";
    this.videoPlaybackErrorKey = "";
    this.updateComplete.then(() => this.querySelector("#library-title")?.focus());
  }

  async moveViewer(direction) {
    if (this.viewerLoading || this.viewerTransitioning) return;
    const currentIndex = this.items.findIndex(
      (item) => entryKey(item) === this.selectedPhotoKey,
    );
    if (currentIndex < 0) return;

    const nextIndex = currentIndex + direction;
    if (nextIndex >= 0 && nextIndex < this.items.length) {
      this.viewerTransitioning = true;
      this.viewerImageVisible = false;
      await this.updateComplete;
      await new Promise((resolve) => window.setTimeout(resolve, VIEWER_FADE_DURATION_MS));
      this.selectedPhotoKey = entryKey(this.items[nextIndex]);
      this.editingRotationKey = "";
      this.viewerTransitioning = false;
      if (!viewerImageSource(this.items[nextIndex])) this.viewerImageVisible = true;
      this.viewerMessage = "";
      this.videoPlaybackErrorKey = "";
      return;
    }

    const hasPreviousPage = direction < 0 && this.offset > 0;
    const hasNextPage = direction > 0 && this.offset + this.items.length < this.total;
    if (!hasPreviousPage && !hasNextPage) return;

    this.viewerLoading = true;
    this.viewerTransitioning = true;
    this.viewerImageVisible = false;
    this.viewerMessage = direction > 0
      ? `Loading next ${mediaNoun(this.mediaType)}…`
      : `Loading previous ${mediaNoun(this.mediaType)}…`;
    await this.updateComplete;
    await new Promise((resolve) => window.setTimeout(resolve, VIEWER_FADE_DURATION_MS));
    await this.loadPhotos({
      offset: Math.max(0, this.offset + direction * this.pageSize),
      viewerTarget: direction > 0 ? "first" : "last",
    });
  }

  handleViewerKeydown(event) {
    if (event.target?.closest?.("video")) return;
    if (event.key === "ArrowRight") {
      event.preventDefault();
      this.moveViewer(1);
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      this.moveViewer(-1);
    } else if (event.key === "Escape") {
      event.preventDefault();
      this.closeViewer();
    }
  }

  currentPhoto() {
    return this.items.find((item) => entryKey(item) === this.selectedPhotoKey) || null;
  }

  handleVideoError(item) {
    this.videoPlaybackErrorKey = entryKey(item);
  }

  layoutViewerImage(image, rotation = Number(image.dataset.rotation)) {
    const stage = image.closest(".viewer-image-stage");
    const body = stage?.parentElement;
    if (stage && body) {
      const columns = window.getComputedStyle(body).gridTemplateColumns.split(/\s+/);
      const maxWidth = Number.parseFloat(columns[1]) || stage.clientWidth;
      const { width, height } = fitImageDimensions(
        image.naturalWidth,
        image.naturalHeight,
        maxWidth,
        body.clientHeight,
        Number(image.dataset.rotation),
      );
      if (width > 0 && height > 0) {
        stage.style.width = `${width}px`;
        stage.style.height = `${height}px`;
        stage.style.aspectRatio = `${width} / ${height}`;
        const sideways = Math.abs(rotation) % 180 === 90;
        image.style.width = `${sideways ? height : width}px`;
        image.style.height = `${sideways ? width : height}px`;
      }
    }
  }

  handleViewerImageLoad(event) {
    this.layoutViewerImage(event.currentTarget);
    this.viewerImageVisible = true;
  }

  renderImageViewer(item, itemIndex) {
    const isVideo = item?.media_type === "video";
    const noun = mediaNoun(this.mediaType);
    const plural = mediaPlural(this.mediaType);
    if (!item) {
      return html`<section class="image-viewer" aria-label=${`${noun} viewer`}>
        <button class="button button-light viewer-back" type="button" @click=${() => this.closeViewer()}>
          Back to ${plural}
        </button>
        <p class="viewer-message" role="status">The selected ${noun} is unavailable.</p>
      </section>`;
    }
    const rotation = rotationFor(this.rotations, item);
    const position = this.offset + itemIndex + 1;
    const imageSource = viewerImageSource(item);
    const previewOnly = requiresPreviewForViewer(item);
    return html`
      <section
        class="image-viewer"
        aria-label=${`${noun[0].toUpperCase()}${noun.slice(1)} viewer`}
        tabindex="-1"
        @keydown=${(event) => this.handleViewerKeydown(event)}
      >
        <header class="viewer-toolbar">
          <button class="button button-light viewer-back" type="button" @click=${() => this.closeViewer()}>
            <span aria-hidden="true">←</span> Back to ${plural}
          </button>
          <div class="viewer-heading">
            <h1>${item.filename}</h1>
            <p>${item.path}</p>
          </div>
          <div class="viewer-tools">
            <span class="viewer-count">${position.toLocaleString()} of ${this.total.toLocaleString()}</span>
            <button
              class="rotation-edit-toggle viewer-edit-toggle"
              type="button"
              title=${isVideo ? "Edit file tags" : "Edit image and tags"}
              aria-label=${isVideo ? `Edit tags for ${item.filename}` : `Edit ${item.filename}`}
              aria-expanded=${this.editingRotationKey === entryKey(item)}
              @click=${() => this.toggleRotationEditor(item)}
            >✎</button>
            <a
              class="download-button viewer-download"
              href=${item.download_url || item.original_url}
              download=${item.filename}
              title=${`Download ${item.filename}`}
              aria-label=${`Download ${item.filename}`}
            ><span aria-hidden="true">↓</span><span class="download-label">Download</span></a>
            ${this.editingRotationKey === entryKey(item)
              ? html`<div class="viewer-edit-panel">
                  ${isVideo ? "" : html`<div class="rotation-controls viewer-rotation-controls" role="group" aria-label=${`Rotate image ${item.filename}`}>
                    <button
                      class="icon-button"
                      type="button"
                      title="Rotate 90° counterclockwise"
                      aria-label=${`Rotate ${item.filename} 90 degrees counterclockwise`}
                      @click=${() => this.rotatePhoto(item, -90)}
                    >↺</button>
                    <button
                      class="icon-button"
                      type="button"
                      title="Rotate 90° clockwise"
                      aria-label=${`Rotate ${item.filename} 90 degrees clockwise`}
                      @click=${() => this.rotatePhoto(item, 90)}
                    >↻</button>
                  </div>`}
                  ${this.renderFileTags(item)}
                </div>`
              : ""}
          </div>
        </header>
        <div class="viewer-body">
          <button
            class="viewer-nav"
            type="button"
            aria-label=${`Previous ${noun}`}
            ?disabled=${position <= 1 || this.viewerLoading || this.viewerTransitioning}
            @click=${() => this.moveViewer(-1)}
          >‹</button>
          <figure class="viewer-image-stage ${isVideo ? "viewer-video-stage" : ""}">
            ${isVideo
              ? html`<video
                  class="viewer-video"
                  src=${item.playback_url || item.play_url || item.original_url}
                  controls
                  preload="metadata"
                  playsinline
                  aria-label=${item.filename}
                  @error=${() => this.handleVideoError(item)}
                ></video>`
              : imageSource
              ? html`<img
                  class="viewer-image"
                  src=${imageSource}
                  alt=${item.filename}
                  data-rotation=${rotation}
                  ?data-visible=${this.viewerImageVisible}
                  @load=${(event) => this.handleViewerImageLoad(event)}
                  @error=${() => { this.viewerImageVisible = true; }}
                />`
              : html`<p class="viewer-image-unavailable">No browser preview is available.</p>`}
          </figure>
          <button
            class="viewer-nav"
            type="button"
            aria-label=${`Next ${noun}`}
            ?disabled=${position >= this.total || this.viewerLoading || this.viewerTransitioning}
            @click=${() => this.moveViewer(1)}
          >›</button>
        </div>
        <p class="viewer-message" role="status">
          ${this.viewerMessage || (isVideo && this.videoPlaybackErrorKey === entryKey(item)
            ? html`This browser could not play this video format. <a href=${item.download_url || item.original_url} download=${item.filename}>Download the preserved original</a>.`
            : previewOnly
            ? html`${imageSource ? "Showing a JPEG preview. " : "No JPEG preview is available. "}<a href=${item.download_url || item.original_url} download=${item.filename}>Download preserved TIFF original</a>.`
            : "")}
        </p>
      </section>
    `;
  }

  async rotatePhoto(item, degrees) {
    const key = rotationKey(item);
    const current = rotationFor(this.rotations, item);
    this.rotations = {
      ...this.rotations,
      [key]: rotateAngle(current, degrees),
    };
    try {
      window.localStorage.setItem(ROTATION_STORAGE_KEY, JSON.stringify(this.rotations));
      this.libraryMessage = "Rotation saved for identical copies in this browser.";
    } catch {
      this.libraryMessage = "Rotation applied for this visit, but browser storage is unavailable.";
    }
    if (this.viewerOpen && entryKey(item) === this.selectedPhotoKey) {
      await this.updateComplete;
      const image = this.querySelector(".viewer-image");
      if (image?.complete && image.naturalWidth > 0) {
        this.layoutViewerImage(image, this.rotations[key]);
      }
    }
  }

  toggleRotationEditor(item) {
    const key = entryKey(item);
    this.editingRotationKey = this.editingRotationKey === key ? "" : key;
  }

  renderPhotoPlaceholder() {
    return html`
      <article class="photo-card photo-card-placeholder" aria-hidden="true">
        <div class="photo-preview">
          <img class="photo-placeholder-image" src="/photo-placeholder.svg" alt="" />
        </div>
        <div class="photo-caption">
          <span class="placeholder-caption-line placeholder-caption-title"></span>
          <span class="placeholder-caption-line placeholder-caption-detail"></span>
        </div>
      </article>
    `;
  }

  handleTagDraft(event, item) {
    this.tagDrafts = { ...this.tagDrafts, [item.sha256]: event.currentTarget.value };
  }

  async addFileTag(event, item) {
    event.preventDefault();
    const value = (this.tagDrafts[item.sha256] || "").trim();
    if (!value || (item.tags || []).some((tag) => tag.toLocaleLowerCase() === value.toLocaleLowerCase())) {
      return;
    }
    const result = await this.saveFileTags(item, [...(item.tags || []), value]);
    if (result) this.tagDrafts = { ...this.tagDrafts, [item.sha256]: "" };
  }

  async removeFileTag(item, tag) {
    await this.saveFileTags(item, (item.tags || []).filter((value) => value !== tag));
  }

  async saveFileTags(item, tags) {
    const digest = item.sha256;
    if (this.savingTagHashes.includes(digest)) return false;
    this.savingTagHashes = [...this.savingTagHashes, digest];
    this.libraryMessage = "Saving tags…";
    try {
      const result = await updateFileTags({
        sha256: digest,
        tags,
        expectedVersion: item.tag_version || 0,
      });
      this.items = this.items.map((current) => current.sha256 === digest
        ? { ...current, tags: result.tags, tag_version: result.version }
        : current);
      this.libraryMessage = "Tags saved for this file and any identical copies.";
      if (this.query.trim()) await this.loadPhotos({ offset: this.offset });
      return true;
    } catch (error) {
      this.libraryMessage = error.message;
      return false;
    } finally {
      this.savingTagHashes = this.savingTagHashes.filter((value) => value !== digest);
    }
  }

  renderFileTags(item) {
    const saving = this.savingTagHashes.includes(item.sha256);
    return html`
      <div class="file-tags" aria-label=${`Tags for ${item.filename}`}>
        <div class="file-tag-list">
          ${(item.tags || []).map((tag) => html`
            <span class="file-tag">
              ${tag}
              <button
                type="button"
                aria-label=${`Remove tag ${tag} from ${item.filename}`}
                ?disabled=${saving}
                @click=${() => this.removeFileTag(item, tag)}
              >×</button>
            </span>
          `)}
        </div>
        <form class="file-tag-form" @submit=${(event) => this.addFileTag(event, item)}>
          <input
            type="text"
            maxlength="50"
            placeholder="Add a tag"
            aria-label=${`Add a tag to ${item.filename}`}
            .value=${this.tagDrafts[item.sha256] || ""}
            ?disabled=${saving}
            @input=${(event) => this.handleTagDraft(event, item)}
          />
          <button type="submit" aria-label=${`Add tag to ${item.filename}`} ?disabled=${saving}>Add</button>
        </form>
      </div>
    `;
  }

  renderPhoto(item) {
    const key = rotationKey(item);
    const rotation = rotationFor(this.rotations, item);
    const editingRotation = this.editingRotationKey === entryKey(item);
    const sourceLabel = this.sources.find((source) => source.source_id === item.source_id)?.volume_label;
    const sourceText = [sourceLabel, item.source_id, item.path].filter(Boolean).join(" · ");
    return html`
      <article class="photo-card">
        <div class="photo-preview">
          <button
            class="photo-link"
            type="button"
            aria-label=${`View ${item.filename} from ${item.source_id}`}
            @click=${() => this.openPhoto(item)}
          >
            <img
              class="photo-thumb"
              src=${item.thumbnail_url || item.original_url}
              alt=${item.filename}
              loading="lazy"
              data-rotation=${rotation}
            />
          </button>
          <a
            class="download-button thumbnail-download"
            href=${item.download_url || item.original_url}
            download=${item.filename}
            title=${`Download ${item.filename}`}
            aria-label=${`Download ${item.filename}`}
            @click=${(event) => event.stopPropagation()}
          ><span aria-hidden="true">↓</span></a>
          <button
            class="rotation-edit-toggle"
            type="button"
            title="Adjust thumbnail rotation"
            aria-label=${`Adjust rotation for ${item.filename}`}
            aria-expanded=${editingRotation}
            @click=${() => this.toggleRotationEditor(item)}
          >✎</button>
          ${editingRotation
            ? html`<div class="rotation-controls" role="group" aria-label=${`Rotate thumbnail ${item.filename}`}>
                <button
                  class="icon-button"
                  type="button"
                  title="Rotate thumbnail 90° counterclockwise"
                  aria-label=${`Rotate thumbnail ${item.filename} 90 degrees counterclockwise`}
                  @click=${() => this.rotatePhoto(item, -90)}
                >↺</button>
                <button
                  class="icon-button"
                  type="button"
                  title="Rotate thumbnail 90° clockwise"
                  aria-label=${`Rotate thumbnail ${item.filename} 90 degrees clockwise`}
                  @click=${() => this.rotatePhoto(item, 90)}
                >↻</button>
              </div>`
            : ""}
        </div>
        <div class="photo-caption">
          <span class="photo-filename">${item.filename}</span>
          <span>${sourceText}</span>
        </div>
      </article>
    `;
  }

  renderVideo(item) {
    const sourceLabel = this.sources.find((source) => source.source_id === item.source_id)?.volume_label;
    const sourceText = [sourceLabel, item.source_id, item.path].filter(Boolean).join(" · ");
    return html`
      <article class="photo-card video-card">
        <div class="photo-preview video-preview">
          <button
            class="video-link"
            type="button"
            aria-label=${`Play ${item.filename} from ${item.source_id}`}
            @click=${() => this.openPhoto(item)}
          >
            ${item.thumbnail_url
              ? html`<img class="video-thumbnail" src=${item.thumbnail_url} alt="" loading="lazy" />`
              : ""}
            <span class="video-play-icon" aria-hidden="true">▶</span>
            <span class="video-play-label">Play video</span>
          </button>
          <a
            class="download-button thumbnail-download"
            href=${item.download_url || item.original_url}
            download=${item.filename}
            title=${`Download ${item.filename}`}
            aria-label=${`Download ${item.filename}`}
            @click=${(event) => event.stopPropagation()}
          ><span aria-hidden="true">↓</span></a>
        </div>
        <div class="photo-caption">
          <span class="photo-filename">${item.filename}</span>
          <span>${sourceText}</span>
        </div>
      </article>
    `;
  }

  goToPage(page) {
    const pageCount = Math.ceil(this.total / this.pageSize);
    if (!Number.isInteger(page) || page < 1 || page > pageCount) return;
    this.items = [];
    return this.loadPhotos({ offset: (page - 1) * this.pageSize });
  }

  handlePageJump(event) {
    event.preventDefault();
    const page = Number(new FormData(event.currentTarget).get("page"));
    this.goToPage(page);
  }

  render() {
    const signedIn = this.view === "library" && !this.mockMode;
    const libraryVisible = this.view === "library";
    const preview = this.view === "preview" || this.view === "signed-out";
    const notPublished = this.view === "library" && !this.items.length && this.total === 0;
    const viewerPhoto = this.viewerOpen ? this.currentPhoto() : null;
    const viewerIndex = viewerPhoto
      ? this.items.findIndex((item) => entryKey(item) === this.selectedPhotoKey)
      : -1;
    const pageCount = Math.max(1, Math.ceil(this.total / this.pageSize));
    const currentPage = Math.floor(this.offset / this.pageSize) + 1;
    return html`
      <main class="shell ${this.viewerOpen ? "shell-viewer-active" : ""}">
        <header class="topbar" ?hidden=${this.viewerOpen}>
          <a class="wordmark" href="/" aria-label="Family Archive home">
            <span class="mark" aria-hidden="true">F</span>
            <span>Family Archive</span>
          </a>
          ${this.mockMode
            ? html`<span class="mock-mode-badge">Local mock</span>`
            : signedIn
            ? html`<button class="button button-light" type="button" @click=${() => this.signOut()}>Sign out</button>`
            : html`<button class="button button-dark" type="button" @click=${() => this.signIn()}>Sign in</button>`}
        </header>

        ${preview
          ? html`
              <section class="welcome" aria-labelledby="welcome-title">
                <p class="eyebrow">A family collection</p>
                <h1 id="welcome-title">Memories, kept together.</h1>
                <p class="intro">
                  A quiet home for the photographs, films, and recordings saved from family archives.
                </p>
                <div class="welcome-actions">
                  <button class="button button-dark" type="button" @click=${() => this.signIn()}>
                    Sign in to your library <span aria-hidden="true">→</span>
                  </button>
                  <span class="privacy-note">Private, family-only access</span>
                </div>
              </section>
            `
          : ""}

        ${this.showStatus && !this.viewerOpen
          ? html`
              <section class="account-panel" aria-live="polite">
                <div class="account-copy">
                  <p class="eyebrow">${signedIn ? "Your library" : this.mockMode ? "Local mock" : "Preview"}</p>
                  <h2>${this.statusTitle}</h2>
                  <p>${this.statusMessage}</p>
                </div>
                <div class="status-indicator" data-state=${this.statusState} aria-hidden="true"></div>
              </section>
            `
          : ""}

        ${libraryVisible
          ? html`
              <section class="library-view" aria-labelledby="library-title" ?hidden=${this.viewerOpen}>
                <div class="library-heading">
                  <div>
                    <p class="eyebrow">From your family archive</p>
                    <h2 id="library-title" tabindex="-1">
                      ${this.mediaType === "video" ? "Videos" : "Photographs"}
                    </h2>
                  </div>
                  <p class="library-count">
                    ${this.total.toLocaleString()} ${this.mediaType === "video"
                      ? (this.total === 1 ? "video" : "videos")
                      : (this.total === 1 ? "photograph" : "photographs")}
                  </p>
                </div>
                <div class="library-filters">
                  <label class="search-field">
                    <span class="visually-hidden">Search photographs</span>
                    <span class="search-icon" aria-hidden="true">⌕</span>
                    <input
                      type="search"
                      placeholder="Search names, folders, or discs"
                      .value=${this.query}
                      @input=${(event) => this.handleSearch(event)}
                    />
                  </label>
                  <details class="source-filter">
                    <summary aria-label="Filter by source disc">
                      ${this.sourceIds.length === 0
                        ? "All discs"
                        : `${this.sourceIds.length} disc${this.sourceIds.length === 1 ? "" : "s"} selected`}
                      <span aria-hidden="true">⌄</span>
                    </summary>
                    <div class="source-options" role="group" aria-label="Filter by source disc">
                      <button
                        class="source-clear"
                        type="button"
                        ?disabled=${this.sourceIds.length === 0}
                        @click=${() => this.clearSources()}
                      >Clear selection · show all discs</button>
                      ${this.sources.map((source) => html`
                        <label class="source-option">
                          <input
                            type="checkbox"
                            value=${source.source_id}
                            .checked=${this.sourceIds.includes(source.source_id)}
                            @change=${(event) => this.handleSource(event)}
                          />
                          <span>${source.volume_label
                            ? `${source.volume_label} · ${source.source_id}`
                            : source.source_id}</span>
                        </label>
                      `)}
                    </div>
                  </details>
                </div>
                <div class="library-options">
                  <div class="view-switch media-switch" role="group" aria-label="Media type">
                    <button
                      class="view-button ${this.mediaType === "photo" ? "selected" : ""}"
                      type="button"
                      aria-pressed=${this.mediaType === "photo"}
                      @click=${() => this.selectMediaType("photo")}
                    >Photos</button>
                    <button
                      class="view-button ${this.mediaType === "video" ? "selected" : ""}"
                      type="button"
                      aria-pressed=${this.mediaType === "video"}
                      @click=${() => this.selectMediaType("video")}
                    >Videos</button>
                  </div>
                  <label class="page-size-filter">
                    <span>${this.mediaType === "video" ? "Videos per page" : "Photos per page"}</span>
                    <select .value=${String(this.pageSize)} @change=${(event) => this.handlePageSize(event)}>
                      ${PAGE_SIZE_OPTIONS.map(
                        (count) => html`<option value=${count}>${count}</option>`,
                      )}
                    </select>
                  </label>
                  <label class="sort-filter">
                    <span>Sort by</span>
                    <select .value=${this.sort} @change=${(event) => this.handleSort(event)}>
                      <option value="archive">Archive order</option>
                      <option value="newest">File date (newest first)</option>
                      <option value="oldest">File date (oldest first)</option>
                    </select>
                  </label>
                  <div class="view-switch" role="group" aria-label=${`${this.mediaType === "video" ? "Video" : "Photo"} layout`}>
                    <button
                      class="view-button ${this.layout === "grid" ? "selected" : ""}"
                      type="button"
                      aria-pressed=${this.layout === "grid"}
                      @click=${() => this.setLayout("grid")}
                    >Grid</button>
                    <button
                      class="view-button ${this.layout === "list" ? "selected" : ""}"
                      type="button"
                      aria-pressed=${this.layout === "list"}
                      @click=${() => this.setLayout("list")}
                    >List</button>
                  </div>
                </div>
                <p class="library-message" role="status">${this.libraryMessage}</p>
                ${notPublished
                  ? ""
                  : html`<div class="photo-grid ${this.layout === "list" ? "photo-grid-list" : ""}">
                      ${this.items.map((item) => this.mediaType === "video"
                        ? this.renderVideo(item)
                        : this.renderPhoto(item))}
                      ${this.loading && this.items.length === 0
                        ? Array.from(
                            { length: placeholderCount(this.total, this.offset, this.pageSize) },
                            () => this.renderPhotoPlaceholder(),
                          )
                        : ""}
                    </div>`}
                ${this.total > 0 && !notPublished
                  ? html`
                      <nav class="pagination" aria-label=${`${this.mediaType === "video" ? "Video" : "Photograph"} pages`}>
                        <div class="pagination-controls">
                          <button
                            class="pagination-arrow"
                            type="button"
                            aria-label="First page"
                            title="First page"
                            ?disabled=${currentPage === 1}
                            @click=${() => this.goToPage(1)}
                          >«</button>
                          <button
                            class="pagination-arrow"
                            type="button"
                            aria-label="Previous page"
                            title="Previous page"
                            ?disabled=${currentPage === 1}
                            @click=${() => this.goToPage(currentPage - 1)}
                          >‹</button>
                          <div class="pagination-pages" aria-label="Choose a page">
                            ${pageTokens(currentPage, pageCount).map((page) => typeof page === "number"
                              ? html`<button
                                  class="pagination-page ${page === currentPage ? "selected" : ""}"
                                  type="button"
                                  aria-label=${`Page ${page}`}
                                  aria-current=${page === currentPage ? "page" : null}
                                  ?disabled=${page === currentPage}
                                  @click=${() => this.goToPage(page)}
                                >${page}</button>`
                              : html`<span class="pagination-ellipsis" aria-hidden="true">…</span>`)}
                          </div>
                          <button
                            class="pagination-arrow"
                            type="button"
                            aria-label="Next page"
                            title="Next page"
                            ?disabled=${currentPage >= pageCount}
                            @click=${() => this.goToPage(currentPage + 1)}
                          >›</button>
                          <button
                            class="pagination-arrow"
                            type="button"
                            aria-label="Last page"
                            title="Last page"
                            ?disabled=${currentPage >= pageCount}
                            @click=${() => this.goToPage(pageCount)}
                          >»</button>
                        </div>
                        <div class="pagination-details">
                          <span class="pagination-summary">
                            Showing ${this.offset + 1}–${Math.min(this.offset + this.items.length, this.total)}
                            of ${this.total.toLocaleString()} · Page ${currentPage} of ${pageCount}
                          </span>
                          <form class="page-jump" @submit=${(event) => this.handlePageJump(event)}>
                            <label for="page-jump-input">Go to page</label>
                            <input
                              id="page-jump-input"
                              name="page"
                              type="number"
                              min="1"
                              max=${pageCount}
                              .value=${String(currentPage)}
                              required
                            />
                            <button class="button button-light" type="submit">Go</button>
                          </form>
                        </div>
                      </nav>
                    `
                  : ""}
              </section>
            `
          : ""}

        ${this.viewerOpen ? this.renderImageViewer(viewerPhoto, viewerIndex) : ""}

        ${preview && !this.viewerOpen
          ? html`
              <section class="preview" aria-label="Library preview">
                <div class="preview-heading">
                  <div>
                    <p class="eyebrow">A place for the little things</p>
                    <h2>Stories worth finding again</h2>
                  </div>
                  <span class="preview-label">Library preview</span>
                </div>
                <div class="memory-grid" aria-hidden="true">
                  <div class="memory memory-sun"><span>Summer days</span></div>
                  <div class="memory memory-table"><span>At home</span></div>
                  <div class="memory memory-blue"><span>By the water</span></div>
                  <div class="memory memory-field"><span>Out together</span></div>
                </div>
                <p class="preview-caption">
                  The signed-in connection is ready. Browsing will appear here when the library is published.
                </p>
              </section>
            `
          : ""}

        <footer ?hidden=${this.viewerOpen}>
          <span>Made for the stories we share.</span>
          <span>Original archive files stay preserved.</span>
        </footer>
      </main>
    `;
  }
}

customElements.define("family-archive-app", FamilyArchiveApp);
