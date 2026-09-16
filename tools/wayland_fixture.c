/* M1 feasibility fixture: native Wayland pixels and actual protocol receipts. */
#define _GNU_SOURCE
#include <wayland-client.h>
#include <xkbcommon/xkbcommon.h>
#include "xdg-shell-client-protocol.h"
#include "presentation-time-client-protocol.h"
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

static struct wl_display *display;
static struct wl_compositor *compositor;
static struct wl_shm *shm;
static struct xdg_wm_base *shell;
static struct wp_presentation *presentation;
static struct wl_surface *surface;
static struct xdg_surface *xdg_surface;
static struct wl_keyboard *keyboard;
static struct wl_pointer *pointer;
static struct xkb_context *xkb_context;
static struct xkb_keymap *keymap;
static struct xkb_state *xkb_state;
static const char *generation;
static bool running = true, configured, pending, dirty;
static unsigned long event_seq, revision;
static unsigned state_value, output_count, output_width, output_height, output_scale, output_transform;
static int width = 640, height = 360;
static double pointer_x, pointer_y;
static const char *next_source = "initial";
static uint32_t presentation_clock;
static struct wl_output *only_output;
static char output_name[128] = "unknown";
static unsigned long control_id;

static unsigned long long monotonic_ns(void) {
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return (unsigned long long)ts.tv_sec * 1000000000ULL + ts.tv_nsec;
}
static void event(const char *type) {
    printf("{\"generation\":\"%s\",\"fixture_pid\":%d,\"seq\":%lu,\"monotonic_ns\":%llu,\"event\":\"%s\"",
           generation, getpid(), ++event_seq, monotonic_ns(), type);
}
static void quoted(const char *s) {
    putchar('"');
    for (const unsigned char *p = (const unsigned char *)s; *p; ++p) {
        if (*p == '"' || *p == '\\') printf("\\%c", *p);
        else if (*p < 32) printf("\\u%04x", *p);
        else putchar(*p);
    }
    putchar('"');
}
static void die(const char *message) {
    event("error"); printf(",\"message\":"); quoted(message); puts("}"); exit(1);
}
struct buffer { struct wl_buffer *wl; void *pixels; size_t size; };
static void release(void *data, struct wl_buffer *wl) {
    struct buffer *b = data; wl_buffer_destroy(wl); munmap(b->pixels, b->size); free(b);
}
static const struct wl_buffer_listener buffer_listener = { .release = release };
struct frame { unsigned long revision; unsigned state; uint32_t checksum; const char *source; unsigned long control_id; bool output_matched; bool sync_received; };
static void render(void);
static void sync_output(void *data, struct wp_presentation_feedback *f, struct wl_output *output) {
    (void)f; struct frame *frame = data; frame->output_matched = output == only_output; frame->sync_received = true;
}
static void presented(void *data, struct wp_presentation_feedback *f, uint32_t hi, uint32_t lo,
                      uint32_t ns, uint32_t refresh, uint32_t seq_hi, uint32_t seq_lo, uint32_t flags) {
    struct frame *frame = data;
    event("presented");
    printf(",\"control_id\":%lu,\"sync_output_received\":%s,\"output_matched\":%s,\"sole_output_name\":", frame->control_id, frame->sync_received ? "true" : "false", frame->output_matched ? "true" : "false"); quoted(output_name);
    printf(",\"revision\":%lu,\"state\":%u,\"source\":\"%s\",\"checksum\":%u,\"clock_id\":%u,\"presentation_seconds\":%llu,\"presentation_ns\":%u,\"refresh_ns\":%u,\"presentation_seq\":%llu,\"flags\":%u}\n",
           frame->revision, frame->state, frame->source, frame->checksum, presentation_clock,
           ((unsigned long long)hi << 32) | lo, ns, refresh, ((unsigned long long)seq_hi << 32) | seq_lo, flags);
    wp_presentation_feedback_destroy(f); free(frame); pending = false; if (dirty) render();
}
static void discarded(void *data, struct wp_presentation_feedback *f) {
    struct frame *frame = data; event("discarded");
    printf(",\"control_id\":%lu", frame->control_id);
    printf(",\"revision\":%lu,\"state\":%u,\"source\":\"%s\"}\n", frame->revision, frame->state, frame->source);
    wp_presentation_feedback_destroy(f); free(frame); pending = false; if (dirty) render();
}
static const struct wp_presentation_feedback_listener feedback_listener = {
    .sync_output = sync_output, .presented = presented, .discarded = discarded
};
static void render(void) {
    if (!configured || pending || !dirty) return;
    struct buffer *b = calloc(1, sizeof(*b));
    if (!b) die("allocation failed");
    b->size = (size_t)width * height * 4;
    int fd = memfd_create("kde-agent-fixture", MFD_CLOEXEC);
    if (fd < 0 || ftruncate(fd, b->size)) die("shared memory failed");
    b->pixels = mmap(NULL, b->size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (b->pixels == MAP_FAILED) die("mapping failed");
    uint32_t *pixels = b->pixels, checksum = 2166136261u;
    const uint32_t colors[] = {0x00204080u, 0x00e06020u, 0x0030c080u, 0x009040c0u};
    for (int y = 0; y < height; ++y) for (int x = 0; x < width; ++x) {
        uint32_t color = colors[((x >= width / 2) + 2 * (y >= height / 2) + state_value) % 4];
        if (y >= 16 && y < 48 && x >= 16 && x < 272)
            color = (state_value & (1u << ((x - 16) / 8))) ? 0x00ffffffu : 0;
        pixels[y * width + x] = color; checksum = (checksum ^ color) * 16777619u;
    }
    struct wl_shm_pool *pool = wl_shm_create_pool(shm, fd, b->size);
    b->wl = wl_shm_pool_create_buffer(pool, 0, width, height, width * 4, WL_SHM_FORMAT_XRGB8888);
    wl_shm_pool_destroy(pool); close(fd); wl_buffer_add_listener(b->wl, &buffer_listener, b);
    struct frame *frame = calloc(1, sizeof(*frame));
    if (!frame) die("allocation failed");
    *frame = (struct frame){++revision, state_value, checksum, next_source, control_id, false, false};
    struct wp_presentation_feedback *feedback = wp_presentation_feedback(presentation, surface);
    wp_presentation_feedback_add_listener(feedback, &feedback_listener, frame);
    wl_surface_attach(surface, b->wl, 0, 0); wl_surface_damage_buffer(surface, 0, 0, width, height);
    wl_surface_commit(surface); pending = true; dirty = false;
    event("committed"); printf(",\"control_id\":%lu", frame->control_id); printf(",\"revision\":%lu,\"state\":%u,\"source\":\"%s\",\"checksum\":%u,\"width\":%d,\"height\":%d}\n",
                              frame->revision, frame->state, frame->source, checksum, width, height);
}
static void ping(void *data, struct xdg_wm_base *base, uint32_t serial) { (void)data; xdg_wm_base_pong(base, serial); }
static const struct xdg_wm_base_listener shell_listener = { .ping = ping };
static void configure(void *data, struct xdg_surface *s, uint32_t serial) {
    (void)data; xdg_surface_ack_configure(s, serial); if (!configured) dirty = true; configured = true; render();
}
static const struct xdg_surface_listener surface_listener = { .configure = configure };
static void top_configure(void *data, struct xdg_toplevel *top, int32_t w, int32_t h, struct wl_array *states) {
    (void)data; (void)top; (void)states;
    if (w > 0 && h > 0) { if (w > 1280 || h > 720) die("unexpected client size"); if (width != w || height != h) dirty = true; width = w; height = h; }
}
static void top_close(void *data, struct xdg_toplevel *top) { (void)data; (void)top; event("close"); puts("}"); running = false; }
static void bounds(void *data, struct xdg_toplevel *top, int32_t w, int32_t h) { (void)data; (void)top; (void)w; (void)h; }
static void wm_capabilities(void *data, struct xdg_toplevel *top, struct wl_array *caps) { (void)data; (void)top; (void)caps; }
static const struct xdg_toplevel_listener top_listener = { .configure = top_configure, .close = top_close, .configure_bounds = bounds, .wm_capabilities = wm_capabilities };
static void clock_id(void *data, struct wp_presentation *p, uint32_t id) { (void)data; (void)p; presentation_clock = id; }
static const struct wp_presentation_listener presentation_listener = { .clock_id = clock_id };
static void geometry(void *data, struct wl_output *o, int32_t x, int32_t y, int32_t pw, int32_t ph, int32_t sub, const char *make, const char *model, int32_t transform) {
    (void)data; (void)o; (void)x; (void)y; (void)pw; (void)ph; (void)sub; (void)make; (void)model; output_transform = transform;
}
static void mode(void *data, struct wl_output *o, uint32_t flags, int32_t w, int32_t h, int32_t refresh) {
    (void)data; (void)o; (void)refresh; if (flags & WL_OUTPUT_MODE_CURRENT) { output_width = w; output_height = h; }
}
static void done(void *data, struct wl_output *o) { (void)data; (void)o; }
static void scale(void *data, struct wl_output *o, int32_t factor) { (void)data; (void)o; output_scale = factor; }
static void name(void *data, struct wl_output *o, const char *n) { (void)data; (void)o; snprintf(output_name, sizeof(output_name), "%s", n); event("output_name"); printf(",\"name\":"); quoted(n); puts("}"); }
static void description(void *data, struct wl_output *o, const char *n) { (void)data; (void)o; (void)n; }
static const struct wl_output_listener output_listener = { .geometry = geometry, .mode = mode, .done = done, .scale = scale, .name = name, .description = description };
static void keyboard_map(void *data, struct wl_keyboard *k, uint32_t format, int32_t fd, uint32_t size) {
    (void)data; (void)k;
    if (format != WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1 || !size) die("unsupported keymap");
    char *map = mmap(NULL, size, PROT_READ, MAP_PRIVATE, fd, 0);
    if (map == MAP_FAILED) die("keymap mapping failed");
    if (xkb_state) xkb_state_unref(xkb_state);
    if (keymap) xkb_keymap_unref(keymap);
    keymap = xkb_keymap_new_from_string(xkb_context, map, XKB_KEYMAP_FORMAT_TEXT_V1, XKB_KEYMAP_COMPILE_NO_FLAGS);
    munmap(map, size); close(fd); if (!keymap) die("invalid keymap");
    xkb_state = xkb_state_new(keymap); if (!xkb_state) die("invalid xkb state");
}
static void keyboard_enter(void *data, struct wl_keyboard *k, uint32_t serial, struct wl_surface *s, struct wl_array *keys) {
    (void)data; (void)k; (void)s; event("keyboard_enter"); printf(",\"serial\":%u,\"held_count\":%zu}\n", serial, keys->size / sizeof(uint32_t));
}
static void keyboard_leave(void *data, struct wl_keyboard *k, uint32_t serial, struct wl_surface *s) {
    (void)data; (void)k; (void)s; event("keyboard_leave"); printf(",\"serial\":%u}\n", serial);
}
static void key(void *data, struct wl_keyboard *k, uint32_t serial, uint32_t time, uint32_t code, uint32_t state) {
    (void)data; (void)k; char text[128] = "";
    xkb_keysym_t sym = XKB_KEY_NoSymbol;
    if (xkb_state) { sym = xkb_state_key_get_one_sym(xkb_state, code + 8); xkb_state_key_get_utf8(xkb_state, code + 8, text, sizeof(text)); }
    event("key"); printf(",\"source\":\"wayland\",\"serial\":%u,\"time_ms\":%u,\"key\":%u,\"state\":%u,\"keysym\":%u,\"text\":", serial, time, code, state, sym); quoted(text); puts("}");
    ++state_value; dirty = true; next_source = "input"; control_id = 0; render();
}
static void modifiers(void *data, struct wl_keyboard *k, uint32_t serial, uint32_t depressed, uint32_t latched, uint32_t locked, uint32_t group) {
    (void)data; (void)k; if (xkb_state) xkb_state_update_mask(xkb_state, depressed, latched, locked, 0, 0, group);
    event("modifiers"); printf(",\"serial\":%u,\"depressed\":%u,\"latched\":%u,\"locked\":%u,\"group\":%u}\n", serial, depressed, latched, locked, group);
}
static void repeat(void *data, struct wl_keyboard *k, int32_t rate, int32_t delay) { (void)data; (void)k; (void)rate; (void)delay; }
static const struct wl_keyboard_listener keyboard_listener = { .keymap = keyboard_map, .enter = keyboard_enter, .leave = keyboard_leave, .key = key, .modifiers = modifiers, .repeat_info = repeat };
static void pointer_enter(void *data, struct wl_pointer *p, uint32_t serial, struct wl_surface *s, wl_fixed_t x, wl_fixed_t y) {
    (void)data; (void)p; (void)s; pointer_x = wl_fixed_to_double(x); pointer_y = wl_fixed_to_double(y);
    event("pointer_enter"); printf(",\"serial\":%u,\"x\":%.3f,\"y\":%.3f}\n", serial, pointer_x, pointer_y);
}
static void pointer_leave(void *data, struct wl_pointer *p, uint32_t serial, struct wl_surface *s) { (void)data; (void)p; (void)s; event("pointer_leave"); printf(",\"serial\":%u}\n", serial); }
static void motion(void *data, struct wl_pointer *p, uint32_t time, wl_fixed_t x, wl_fixed_t y) {
    (void)data; (void)p; pointer_x = wl_fixed_to_double(x); pointer_y = wl_fixed_to_double(y);
    event("motion"); printf(",\"time_ms\":%u,\"x\":%.3f,\"y\":%.3f}\n", time, pointer_x, pointer_y);
}
static void button(void *data, struct wl_pointer *p, uint32_t serial, uint32_t time, uint32_t code, uint32_t state) {
    (void)data; (void)p; event("button"); printf(",\"source\":\"wayland\",\"serial\":%u,\"time_ms\":%u,\"button\":%u,\"state\":%u,\"x\":%.3f,\"y\":%.3f}\n", serial, time, code, state, pointer_x, pointer_y);
    ++state_value; dirty = true; next_source = "input"; control_id = 0; render();
}
static void axis(void *d, struct wl_pointer *p, uint32_t t, uint32_t a, wl_fixed_t v) { (void)d; (void)p; event("axis"); printf(",\"time_ms\":%u,\"axis\":%u,\"value\":%.3f}\n", t, a, wl_fixed_to_double(v)); }
/* Bind pointer v5: all events through v5 have handlers. */
static void pointer_frame(void *d, struct wl_pointer *p) { (void)d; (void)p; }
static void axis_source(void *d, struct wl_pointer *p, uint32_t s) { (void)d; (void)p; (void)s; }
static void axis_stop(void *d, struct wl_pointer *p, uint32_t t, uint32_t a) { (void)d; (void)p; (void)t; (void)a; }
static void axis_discrete(void *d, struct wl_pointer *p, uint32_t a, int32_t s) { (void)d; (void)p; (void)a; (void)s; }
static const struct wl_pointer_listener pointer_listener = { .enter = pointer_enter, .leave = pointer_leave, .motion = motion, .button = button, .axis = axis, .frame = pointer_frame, .axis_source = axis_source, .axis_stop = axis_stop, .axis_discrete = axis_discrete };
static void capabilities(void *data, struct wl_seat *seat, uint32_t caps) {
    (void)data;
    if ((caps & WL_SEAT_CAPABILITY_KEYBOARD) && !keyboard) { keyboard = wl_seat_get_keyboard(seat); wl_keyboard_add_listener(keyboard, &keyboard_listener, NULL); }
    if (!(caps & WL_SEAT_CAPABILITY_KEYBOARD) && keyboard) { wl_keyboard_release(keyboard); keyboard = NULL; }
    if ((caps & WL_SEAT_CAPABILITY_POINTER) && !pointer) { pointer = wl_seat_get_pointer(seat); wl_pointer_add_listener(pointer, &pointer_listener, NULL); }
    if (!(caps & WL_SEAT_CAPABILITY_POINTER) && pointer) { wl_pointer_release(pointer); pointer = NULL; }
    event("seat_capabilities"); printf(",\"capabilities\":%u}\n", caps);
}
static void seat_name(void *data, struct wl_seat *seat, const char *n) { (void)data; (void)seat; (void)n; }
static const struct wl_seat_listener seat_listener = { .capabilities = capabilities, .name = seat_name };
static void global(void *data, struct wl_registry *registry, uint32_t id, const char *interface, uint32_t version) {
    (void)data; event("global"); printf(",\"interface\":"); quoted(interface); printf(",\"version\":%u}\n", version);
    if (!strcmp(interface, "wl_compositor")) compositor = wl_registry_bind(registry, id, &wl_compositor_interface, version < 4 ? version : 4);
    else if (!strcmp(interface, "wl_shm")) shm = wl_registry_bind(registry, id, &wl_shm_interface, 1);
    else if (!strcmp(interface, "xdg_wm_base")) { shell = wl_registry_bind(registry, id, &xdg_wm_base_interface, version < 4 ? version : 4); xdg_wm_base_add_listener(shell, &shell_listener, NULL); }
    else if (!strcmp(interface, "wp_presentation")) { presentation = wl_registry_bind(registry, id, &wp_presentation_interface, 1); wp_presentation_add_listener(presentation, &presentation_listener, NULL); }
    else if (!strcmp(interface, "wl_output")) { ++output_count; struct wl_output *o = wl_registry_bind(registry, id, &wl_output_interface, version < 4 ? version : 4); only_output = o; wl_output_add_listener(o, &output_listener, NULL); }
    else if (!strcmp(interface, "wl_seat")) { struct wl_seat *seat = wl_registry_bind(registry, id, &wl_seat_interface, version < 5 ? version : 5); wl_seat_add_listener(seat, &seat_listener, NULL); }
}
static void global_remove(void *data, struct wl_registry *registry, uint32_t id) { (void)data; (void)registry; event("global_remove"); printf(",\"id\":%u}\n", id); }
static const struct wl_registry_listener registry_listener = { .global = global, .global_remove = global_remove };
int main(void) {
    generation = getenv("HARNESS_GENERATION");
    if (!generation || strlen(generation) != 32 || strspn(generation, "0123456789abcdef") != 32) return 2;
    setvbuf(stdout, NULL, _IOLBF, 0);
    if (!getenv("WAYLAND_DISPLAY") || !getenv("XDG_RUNTIME_DIR") || !getenv("DBUS_SESSION_BUS_ADDRESS")) die("missing private endpoints");
    display = wl_display_connect(getenv("WAYLAND_DISPLAY")); if (!display) die("private Wayland connection failed");
    xkb_context = xkb_context_new(XKB_CONTEXT_NO_FLAGS); if (!xkb_context) die("xkb context failed");
    struct wl_registry *registry = wl_display_get_registry(display); wl_registry_add_listener(registry, &registry_listener, NULL);
    if (wl_display_roundtrip(display) < 0 || wl_display_roundtrip(display) < 0) die("registry failed");
    if (!compositor || !shm || !shell || !presentation) die("required Wayland/presentation global absent");
    event("output"); printf(",\"count\":%u,\"width\":%u,\"height\":%u,\"scale\":%u,\"transform\":%u}\n", output_count, output_width, output_height, output_scale, output_transform);
    if (output_count != 1 || output_width != 1280 || output_height != 720 || output_scale != 1 || output_transform != 0) die("unexpected output configuration");
    surface = wl_compositor_create_surface(compositor); xdg_surface = xdg_wm_base_get_xdg_surface(shell, surface);
    xdg_surface_add_listener(xdg_surface, &surface_listener, NULL);
    struct xdg_toplevel *top = xdg_surface_get_toplevel(xdg_surface); xdg_toplevel_add_listener(top, &top_listener, NULL);
    xdg_toplevel_set_title(top, "KDE Agent Native Fixture"); xdg_toplevel_set_app_id(top, "org.kde_agent.fixture");
    xdg_toplevel_set_min_size(top, 640, 360); xdg_toplevel_set_max_size(top, 640, 360);
    wl_surface_commit(surface);
    char input[128]; size_t used = 0;
    while (running) {
        if (wl_display_dispatch_pending(display) < 0) die("Wayland dispatch failed");
        if (wl_display_flush(display) < 0 && errno != EAGAIN) die("Wayland flush failed");
        struct pollfd fds[2] = {{wl_display_get_fd(display), POLLIN, 0}, {STDIN_FILENO, POLLIN, 0}};
        if (poll(fds, 2, 100) < 0) { if (errno == EINTR) continue; die("poll failed"); }
        if (fds[0].revents & (POLLERR | POLLHUP)) die("Wayland disconnected");
        if ((fds[0].revents & POLLIN) && wl_display_dispatch(display) < 0) die("Wayland disconnected");
        if (fds[1].revents & (POLLERR | POLLHUP)) break;
        if (fds[1].revents & POLLIN) {
            ssize_t n = read(STDIN_FILENO, input + used, sizeof(input) - used - 1); if (n <= 0) break; used += n; input[used] = 0;
            char *newline;
            while ((newline = strchr(input, '\n'))) {
                *newline = 0;
                if (!strcmp(input, "close")) { running = false; event("close"); puts("}"); }
                else {
                    unsigned value; unsigned long request_control_id; char extra;
                    if (sscanf(input, "state %u %lu%c", &value, &request_control_id, &extra) != 2) die("invalid control command");
                    if (pending || dirty) { event("control_busy"); printf(",\"control_id\":%lu}\n", request_control_id); }
                    else { state_value = value; control_id = request_control_id; next_source = "control"; dirty = true; render(); }
                }
                size_t consumed = (size_t)(newline - input) + 1; memmove(input, input + consumed, used - consumed); used -= consumed; input[used] = 0;
            }
            if (used == sizeof(input) - 1) die("control message too long");
        }
    }
    wl_display_disconnect(display); return 0;
}
