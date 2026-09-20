/* M1 feasibility fixture: native Wayland pixels and actual protocol receipts. */
#define _GNU_SOURCE
#include <wayland-client.h>
#include <xkbcommon/xkbcommon.h>
#include "xdg-shell-client-protocol.h"
#include "presentation-time-client-protocol.h"
#include <errno.h>
#include <fcntl.h>
#include <linux/input-event-codes.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static struct wl_display *display;
static struct wl_compositor *compositor;
static struct wl_shm *shm;
static struct xdg_wm_base *shell;
static struct wp_presentation *presentation;
/* Fixed slots keep callback ownership valid even after a surface is closed. */
struct fixture_surface {
    const char *label;
    const char *role;
    struct wl_surface *wl;
    struct xdg_surface *xdg;
    struct xdg_toplevel *top;
    bool configured, pending, dirty, closed, mapped, activated;
    bool resize_scheduled, destroy_scheduled, resize_done, destroy_done;
    unsigned resize_after_ms, destroy_after_ms, resize_width, resize_height;
    unsigned long revision, control_id;
    unsigned close_requests;
    unsigned long long close_due_ns;
    unsigned state;
    int width, height;
    const char *source;
};
static struct fixture_surface surfaces[3] = {
    {.label = "primary", .role = "primary", .width = 640, .height = 360, .source = "initial"},
    {.label = "sibling", .role = "sibling", .width = 480, .height = 300, .source = "initial"},
    {.label = "dialog", .role = "dialog", .width = 320, .height = 180, .source = "initial"},
};
static struct fixture_surface *keyboard_surface, *pointer_surface;
static bool sibling_window, dialog_window, child_surface, windows_created;
static unsigned child_window_ms;
static pid_t window_child;
static const char *title_mode = "normal", *app_id_mode = "normal";
static struct wl_keyboard *keyboard;
static struct wl_pointer *pointer;
static struct xkb_context *xkb_context;
static struct xkb_keymap *keymap;
static struct xkb_state *xkb_state;
static const char *generation;
static bool running = true;
static unsigned long event_seq;
static unsigned output_count, output_width, output_height, output_scale, output_transform;
static double pointer_x, pointer_y;
static uint32_t presentation_clock;
static struct wl_output *only_output;
static char output_name[128] = "unknown";
static bool autonomous;
static unsigned window_delay_ms, exit_after_ms, descendant_ms;
static bool timed_exit;
static int exit_code;
static const char *close_mode = "normal";
static unsigned close_delay_ms;
static struct fixture_surface *confirmation_target;

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
static void surface_event(const char *type, const struct fixture_surface *s) {
    event(type); printf(",\"surface\":"); quoted(s->label);
    printf(",\"role\":"); quoted(s->role);
}
static void input_event(const char *type, const struct fixture_surface *s) {
    if (s) surface_event(type, s);
    else { event(type); printf(",\"surface\":null,\"role\":null"); }
}
static struct fixture_surface *find_surface(struct wl_surface *wl) {
    for (unsigned i = 0; i < 3; ++i) if (surfaces[i].wl == wl) return &surfaces[i];
    return NULL;
}
struct buffer { struct wl_buffer *wl; void *pixels; size_t size; };
static void release(void *data, struct wl_buffer *wl) {
    struct buffer *b = data; wl_buffer_destroy(wl); munmap(b->pixels, b->size); free(b);
}
static const struct wl_buffer_listener buffer_listener = { .release = release };
struct frame { struct fixture_surface *surface; unsigned long revision; unsigned state; uint32_t checksum; const char *source; unsigned long control_id; bool output_matched; bool sync_received; };
static void render(struct fixture_surface *s);
static void create_window(struct fixture_surface *s, struct fixture_surface *parent);
static void sync_output(void *data, struct wp_presentation_feedback *f, struct wl_output *output) {
    (void)f; struct frame *frame = data; frame->output_matched = output == only_output; frame->sync_received = true;
}
static void presented(void *data, struct wp_presentation_feedback *f, uint32_t hi, uint32_t lo,
                      uint32_t ns, uint32_t refresh, uint32_t seq_hi, uint32_t seq_lo, uint32_t flags) {
    struct frame *frame = data;
    surface_event("presented", frame->surface);
    printf(",\"control_id\":%lu,\"sync_output_received\":%s,\"output_matched\":%s,\"sole_output_name\":", frame->control_id, frame->sync_received ? "true" : "false", frame->output_matched ? "true" : "false"); quoted(output_name);
    printf(",\"revision\":%lu,\"state\":%u,\"source\":\"%s\",\"checksum\":%u,\"clock_id\":%u,\"presentation_seconds\":%llu,\"presentation_ns\":%u,\"refresh_ns\":%u,\"presentation_seq\":%llu,\"flags\":%u}\n",
           frame->revision, frame->state, frame->source, frame->checksum, presentation_clock,
           ((unsigned long long)hi << 32) | lo, ns, refresh, ((unsigned long long)seq_hi << 32) | seq_lo, flags);
    struct fixture_surface *s = frame->surface;
    wp_presentation_feedback_destroy(f); free(frame); s->pending = false; if (s->dirty) render(s);
}
static void discarded(void *data, struct wp_presentation_feedback *f) {
    struct frame *frame = data; surface_event("discarded", frame->surface);
    printf(",\"control_id\":%lu", frame->control_id);
    printf(",\"revision\":%lu,\"state\":%u,\"source\":\"%s\"}\n", frame->revision, frame->state, frame->source);
    struct fixture_surface *s = frame->surface;
    wp_presentation_feedback_destroy(f); free(frame); s->pending = false; if (s->dirty) render(s);
}
static const struct wp_presentation_feedback_listener feedback_listener = {
    .sync_output = sync_output, .presented = presented, .discarded = discarded
};
static void render(struct fixture_surface *s) {
    if (s->closed || !s->configured || s->pending || !s->dirty) return;
    struct buffer *b = calloc(1, sizeof(*b));
    if (!b) die("allocation failed");
    b->size = (size_t)s->width * s->height * 4;
    int fd = memfd_create("kde-agent-fixture", MFD_CLOEXEC);
    if (fd < 0 || ftruncate(fd, b->size)) die("shared memory failed");
    b->pixels = mmap(NULL, b->size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (b->pixels == MAP_FAILED) die("mapping failed");
    uint32_t *pixels = b->pixels, checksum = 2166136261u;
    const uint32_t colors[] = {0x00204080u, 0x00e06020u, 0x0030c080u, 0x009040c0u};
    for (int y = 0; y < s->height; ++y) for (int x = 0; x < s->width; ++x) {
        uint32_t color = colors[((x >= s->width / 2) + 2 * (y >= s->height / 2) + s->state) % 4];
        if (y >= 16 && y < 48 && x >= 16 && x < 272)
            color = (s->state & (1u << ((x - 16) / 8))) ? 0x00ffffffu : 0;
        if (s == &surfaces[2] && confirmation_target &&
            x >= 16 && x < s->width - 16 && y >= s->height - 56 && y < s->height - 16)
            color = 0x00ffffffu;
        pixels[y * s->width + x] = color; checksum = (checksum ^ color) * 16777619u;
    }
    struct wl_shm_pool *pool = wl_shm_create_pool(shm, fd, b->size);
    b->wl = wl_shm_pool_create_buffer(pool, 0, s->width, s->height, s->width * 4, WL_SHM_FORMAT_XRGB8888);
    wl_shm_pool_destroy(pool); close(fd); wl_buffer_add_listener(b->wl, &buffer_listener, b);
    struct frame *frame = calloc(1, sizeof(*frame));
    if (!frame) die("allocation failed");
    *frame = (struct frame){s, ++s->revision, s->state, checksum, s->source, s->control_id, false, false};
    struct wp_presentation_feedback *feedback = wp_presentation_feedback(presentation, s->wl);
    wp_presentation_feedback_add_listener(feedback, &feedback_listener, frame);
    wl_surface_attach(s->wl, b->wl, 0, 0); wl_surface_damage_buffer(s->wl, 0, 0, s->width, s->height);
    wl_surface_commit(s->wl); s->pending = true; s->dirty = false;
    if (!s->mapped) {
        s->mapped = true;
        surface_event("map", s); printf(",\"receipt\":\"first_buffer_commit\",\"width\":%d,\"height\":%d}\n", s->width, s->height);
    }
    surface_event("committed", s); printf(",\"control_id\":%lu", frame->control_id); printf(",\"revision\":%lu,\"state\":%u,\"source\":\"%s\",\"checksum\":%u,\"width\":%d,\"height\":%d}\n",
                              frame->revision, frame->state, frame->source, checksum, s->width, s->height);
}
static void ping(void *data, struct xdg_wm_base *base, uint32_t serial) { (void)data; xdg_wm_base_pong(base, serial); }
static const struct xdg_wm_base_listener shell_listener = { .ping = ping };
static void configure(void *data, struct xdg_surface *xdg, uint32_t serial) {
    struct fixture_surface *s = data;
    xdg_surface_ack_configure(xdg, serial);
    surface_event("configure", s);
    printf(",\"serial\":%u,\"width\":%d,\"height\":%d,\"activated\":%s}\n",
           serial, s->width, s->height, s->activated ? "true" : "false");
    if (!s->configured) s->dirty = true;
    s->configured = true; render(s);
}
static const struct xdg_surface_listener surface_listener = { .configure = configure };
static void top_configure(void *data, struct xdg_toplevel *top, int32_t w, int32_t h, struct wl_array *states) {
    struct fixture_surface *s = data; (void)top;
    /* A boolean and count keep receipts bounded even as the protocol grows. */
    if (states->size % sizeof(uint32_t) || states->size > 64 * sizeof(uint32_t))
        die("unexpected toplevel state array");
    s->activated = false;
    uint32_t *state;
    wl_array_for_each(state, states)
        if (*state == XDG_TOPLEVEL_STATE_ACTIVATED) s->activated = true;
    surface_event("toplevel_configure", s);
    printf(",\"width\":%d,\"height\":%d,\"activated\":%s,\"state_count\":%zu}\n",
           w, h, s->activated ? "true" : "false", states->size / sizeof(uint32_t));
    if (w > 0 && h > 0) {
        if (w > 1280 || h > 720) die("unexpected client size");
        if (s->width != w || s->height != h) s->dirty = true;
        s->width = w; s->height = h;
    }
}
static void close_surface(struct fixture_surface *s, const char *source) {
    if (!s->wl || s->closed) return;
    surface_event("close", s); printf(",\"source\":"); quoted(source); puts("}");
    s->closed = true; s->close_due_ns = 0;
    if (keyboard_surface == s) keyboard_surface = NULL;
    if (pointer_surface == s) pointer_surface = NULL;
    xdg_toplevel_destroy(s->top); xdg_surface_destroy(s->xdg); wl_surface_destroy(s->wl);
    s->top = NULL; s->xdg = NULL; s->wl = NULL;
    surface_event("destroy", s); printf(",\"source\":"); quoted(source); puts("}");
    bool any_open = false;
    for (unsigned i = 0; i < 3; ++i) if (surfaces[i].wl) any_open = true;
    if (!any_open) running = false;
}
static void top_close(void *data, struct xdg_toplevel *top) {
    (void)top;
    struct fixture_surface *s = data;
    surface_event("close_requested", s);
    printf(",\"source\":\"compositor\",\"request_count\":%u,\"close_mode\":", ++s->close_requests);
    quoted(close_mode); puts("}");
    if (!strcmp(close_mode, "refuse") || (confirmation_target && s == &surfaces[2])) {
        surface_event("close_refused", s); puts("}");
    } else if (!strcmp(close_mode, "delay")) {
        if (!s->close_due_ns) s->close_due_ns = monotonic_ns() + close_delay_ms * 1000000ULL;
        surface_event("close_delayed", s);
        printf(",\"delay_ms\":%u,\"due_ns\":%llu}\n", close_delay_ms, s->close_due_ns);
    } else if (!strcmp(close_mode, "confirmation")) {
        if (!confirmation_target) {
            confirmation_target = s;
            surfaces[2].role = "confirmation";
            create_window(&surfaces[2], s);
            surface_event("confirmation_opened", &surfaces[2]);
            printf(",\"target\":"); quoted(s->label); puts("}");
        } else {
            surface_event("confirmation_pending", s);
            printf(",\"target\":"); quoted(confirmation_target->label); puts("}");
        }
    } else close_surface(s, "compositor");
}
static bool acknowledge_confirmation(struct fixture_surface *s, const char *source) {
    if (!confirmation_target || s != &surfaces[2] || !s->mapped || s->closed) return false;
    struct fixture_surface *target = confirmation_target;
    surface_event("confirmation_accepted", s);
    printf(",\"source\":"); quoted(source); printf(",\"target\":"); quoted(target->label); puts("}");
    /* One confirmation per fixed dialog slot; remaining siblings close normally. */
    confirmation_target = NULL; close_mode = "normal";
    close_surface(s, "confirmation"); close_surface(target, "confirmation");
    return true;
}
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
    (void)data; (void)k; keyboard_surface = find_surface(s); event("keyboard_enter"); printf(",\"serial\":%u,\"held_count\":%zu}\n", serial, keys->size / sizeof(uint32_t));
}
static void keyboard_leave(void *data, struct wl_keyboard *k, uint32_t serial, struct wl_surface *s) {
    (void)data; (void)k; (void)s; keyboard_surface = NULL; event("keyboard_leave"); printf(",\"serial\":%u}\n", serial);
}
static void key(void *data, struct wl_keyboard *k, uint32_t serial, uint32_t time, uint32_t code, uint32_t state) {
    (void)data; (void)k; char text[128] = "";
    xkb_keysym_t sym = XKB_KEY_NoSymbol;
    if (xkb_state) { sym = xkb_state_key_get_one_sym(xkb_state, code + 8); xkb_state_key_get_utf8(xkb_state, code + 8, text, sizeof(text)); }
    input_event("key", keyboard_surface); printf(",\"source\":\"wayland\",\"serial\":%u,\"time_ms\":%u,\"key\":%u,\"state\":%u,\"keysym\":%u,\"text\":", serial, time, code, state, sym); quoted(text); puts("}");
    if (state == WL_KEYBOARD_KEY_STATE_PRESSED && (sym == XKB_KEY_Return || sym == XKB_KEY_KP_Enter) &&
        acknowledge_confirmation(keyboard_surface, "key")) return;
    struct fixture_surface *s = keyboard_surface ? keyboard_surface : &surfaces[0];
    ++s->state; s->dirty = true; s->source = "input"; s->control_id = 0; render(s);
}
static void modifiers(void *data, struct wl_keyboard *k, uint32_t serial, uint32_t depressed, uint32_t latched, uint32_t locked, uint32_t group) {
    (void)data; (void)k; if (xkb_state) xkb_state_update_mask(xkb_state, depressed, latched, locked, 0, 0, group);
    event("modifiers"); printf(",\"serial\":%u,\"depressed\":%u,\"latched\":%u,\"locked\":%u,\"group\":%u}\n", serial, depressed, latched, locked, group);
}
static void repeat(void *data, struct wl_keyboard *k, int32_t rate, int32_t delay) { (void)data; (void)k; (void)rate; (void)delay; }
static const struct wl_keyboard_listener keyboard_listener = { .keymap = keyboard_map, .enter = keyboard_enter, .leave = keyboard_leave, .key = key, .modifiers = modifiers, .repeat_info = repeat };
static void pointer_enter(void *data, struct wl_pointer *p, uint32_t serial, struct wl_surface *s, wl_fixed_t x, wl_fixed_t y) {
    (void)data; (void)p; pointer_surface = find_surface(s); pointer_x = wl_fixed_to_double(x); pointer_y = wl_fixed_to_double(y);
    event("pointer_enter"); printf(",\"serial\":%u,\"x\":%.3f,\"y\":%.3f}\n", serial, pointer_x, pointer_y);
}
static void pointer_leave(void *data, struct wl_pointer *p, uint32_t serial, struct wl_surface *s) { (void)data; (void)p; (void)s; pointer_surface = NULL; event("pointer_leave"); printf(",\"serial\":%u}\n", serial); }
static void motion(void *data, struct wl_pointer *p, uint32_t time, wl_fixed_t x, wl_fixed_t y) {
    (void)data; (void)p; pointer_x = wl_fixed_to_double(x); pointer_y = wl_fixed_to_double(y);
    input_event("motion", pointer_surface); printf(",\"time_ms\":%u,\"x\":%.3f,\"y\":%.3f}\n", time, pointer_x, pointer_y);
}
static void button(void *data, struct wl_pointer *p, uint32_t serial, uint32_t time, uint32_t code, uint32_t state) {
    (void)data; (void)p; input_event("button", pointer_surface); printf(",\"source\":\"wayland\",\"serial\":%u,\"time_ms\":%u,\"button\":%u,\"state\":%u,\"x\":%.3f,\"y\":%.3f}\n", serial, time, code, state, pointer_x, pointer_y);
    if (pointer_surface == &surfaces[2] && code == BTN_LEFT && state == WL_POINTER_BUTTON_STATE_PRESSED &&
        pointer_x >= 16 && pointer_x < pointer_surface->width - 16 &&
        pointer_y >= pointer_surface->height - 56 && pointer_y < pointer_surface->height - 16 &&
        acknowledge_confirmation(pointer_surface, "button")) return;
    struct fixture_surface *s = pointer_surface ? pointer_surface : &surfaces[0];
    ++s->state; s->dirty = true; s->source = "input"; s->control_id = 0; render(s);
}
static void axis(void *d, struct wl_pointer *p, uint32_t t, uint32_t a, wl_fixed_t v) { (void)d; (void)p; input_event("axis", pointer_surface); printf(",\"time_ms\":%u,\"axis\":%u,\"value\":%.3f}\n", t, a, wl_fixed_to_double(v)); }
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

/* These opt-in controls exercise public launch without the harness stdin pipe.
 * Timers begin after registry/output validation. The detached descendant only
 * sleeps: it retains stdout/stderr and cgroup membership, but no Wayland fd. */
static void usage(void) {
    fprintf(stderr, "usage: wayland-fixture [--autonomous] [--window-delay-ms N] "
            "[--exit-after-ms N] [--exit-code N] [--descendant-ms N]\n"
            "  [--sibling] [--dialog] [--child-window-ms N]\n"
            "  [--close-mode normal|refuse|delay|confirmation] [--close-delay-ms N]\n"
            "  [--resize-after-ms LABEL:MS:WIDTH:HEIGHT] [--destroy-after-ms LABEL:MS]\n"
            "  [--title-mode normal|empty|omitted] [--app-id-mode normal|empty|omitted]\n"
            "Durations: 0..86400000 ms; exit code: 0..255. "
            "--descendant-ms and --child-window-ms must be positive.\n"
            "Non-normal close modes require positive --exit-after-ms; delay requires positive\n"
            "--close-delay-ms. Confirmation reserves the dialog slot (no --dialog).\n"
            "One resize and destroy per enabled primary/sibling/dialog; timers start at started.\n"
            "Resize dimensions: 1..1280 by 1..720; schedule times must follow window delay.\n");
}
static unsigned option_number(const char *value, unsigned maximum) {
    char *end;
    errno = 0;
    unsigned long number = strtoul(value, &end, 10);
    if (!*value || strspn(value, "0123456789") != strlen(value) ||
        errno || *end || number > maximum) { usage(); exit(2); }
    return (unsigned)number;
}
static void schedule_option(const char *value, bool resize) {
    char copy[128], *parts[4];
    if (strlen(value) >= sizeof(copy)) { usage(); exit(2); }
    strcpy(copy, value);
    char *next = copy;
    unsigned count = resize ? 4 : 2;
    for (unsigned i = 0; i < count; ++i) {
        parts[i] = strsep(&next, ":");
        if (!parts[i] || !*parts[i]) { usage(); exit(2); }
    }
    if (next) { usage(); exit(2); }
    struct fixture_surface *s = NULL;
    for (unsigned i = 0; i < 3; ++i)
        if (!strcmp(parts[0], surfaces[i].label)) s = &surfaces[i];
    if (!s || (resize ? s->resize_scheduled : s->destroy_scheduled)) { usage(); exit(2); }
    unsigned after_ms = option_number(parts[1], 86400000);
    if (resize) {
        s->resize_scheduled = true; s->resize_after_ms = after_ms;
        s->resize_width = option_number(parts[2], 1280);
        s->resize_height = option_number(parts[3], 720);
        if (!s->resize_width || !s->resize_height) { usage(); exit(2); }
    } else { s->destroy_scheduled = true; s->destroy_after_ms = after_ms; }
}
static void scheduled_destroy(struct fixture_surface *s, unsigned long long elapsed_ms) {
    s->destroy_done = true;
    surface_event("scheduled_destroy", s);
    printf(",\"after_ms\":%u,\"elapsed_ms\":%llu,\"applied\":%s}\n",
           s->destroy_after_ms, elapsed_ms, s->wl && !s->closed ? "true" : "false");
    close_surface(s, "scheduled");
}
static void run_surface_schedule(struct fixture_surface *s, unsigned long long elapsed_ms) {
    /* Preserve timer order if dispatch is delayed past both deadlines. */
    if (s->destroy_scheduled && !s->destroy_done && elapsed_ms >= s->destroy_after_ms &&
        s->resize_scheduled && s->destroy_after_ms < s->resize_after_ms)
        scheduled_destroy(s, elapsed_ms);
    if (s->resize_scheduled && !s->resize_done && elapsed_ms >= s->resize_after_ms) {
        s->resize_done = true;
        surface_event("scheduled_resize", s);
        printf(",\"after_ms\":%u,\"elapsed_ms\":%llu,\"width\":%u,\"height\":%u,\"applied\":%s}\n",
               s->resize_after_ms, elapsed_ms, s->resize_width, s->resize_height,
               s->wl && !s->closed ? "true" : "false");
        if (s->wl && !s->closed) {
            s->width = s->resize_width; s->height = s->resize_height;
            xdg_toplevel_set_min_size(s->top, s->width, s->height);
            xdg_toplevel_set_max_size(s->top, s->width, s->height);
            s->source = "scheduled_resize"; s->control_id = 0; s->dirty = true;
            render(s);
        }
    }
    if (s->destroy_scheduled && !s->destroy_done && elapsed_ms >= s->destroy_after_ms)
        scheduled_destroy(s, elapsed_ms);
}
static void start_descendant(void) {
    int ready[2];
    if (pipe2(ready, O_CLOEXEC)) die("descendant pipe failed");
    pid_t original = getpid(), intermediate = fork();
    if (intermediate < 0) die("descendant fork failed");
    if (!intermediate) {
        close(ready[0]);
        /* No Wayland calls after fork, and no inherited connection held open. */
        close(wl_display_get_fd(display));
        if (setsid() < 0) _exit(1);
        pid_t descendant = fork();
        if (descendant < 0) _exit(1);
        if (descendant) _exit(0);
        close(STDIN_FILENO);
        event_seq = 0;
        event("descendant_started");
        printf(",\"original_pid\":%d,\"session_id\":%d,\"duration_ms\":%u}\n",
               original, getsid(0), descendant_ms);
        pid_t own_pid = getpid();
        if (write(ready[1], &own_pid, sizeof(own_pid)) != sizeof(own_pid)) _exit(1);
        close(ready[1]);
        struct timespec remaining = {descendant_ms / 1000, (descendant_ms % 1000) * 1000000L};
        while (nanosleep(&remaining, &remaining) < 0) {
            if (errno != EINTR) _exit(1);
        }
        event("descendant_exit"); printf(",\"original_pid\":%d,\"exit_code\":0}\n", original);
        _exit(0);
    }
    close(ready[1]);
    pid_t descendant;
    ssize_t bytes;
    do { bytes = read(ready[0], &descendant, sizeof(descendant)); } while (bytes < 0 && errno == EINTR);
    close(ready[0]);
    int status;
    pid_t reaped;
    do { reaped = waitpid(intermediate, &status, 0); } while (reaped < 0 && errno == EINTR);
    if (bytes != sizeof(descendant) || reaped != intermediate ||
        !WIFEXITED(status) || WEXITSTATUS(status)) die("descendant startup failed");
    event("descendant_spawned"); printf(",\"descendant_pid\":%d,\"intermediate_pid\":%d}\n", descendant, intermediate);
}
static void create_window(struct fixture_surface *s, struct fixture_surface *parent) {
    if (s->wl || s->closed) die("surface slot already used");
    s->wl = wl_compositor_create_surface(compositor);
    s->xdg = xdg_wm_base_get_xdg_surface(shell, s->wl);
    xdg_surface_add_listener(s->xdg, &surface_listener, s);
    s->top = xdg_surface_get_toplevel(s->xdg);
    xdg_toplevel_add_listener(s->top, &top_listener, s);
    if (strcmp(title_mode, "omitted")) xdg_toplevel_set_title(s->top, !strcmp(title_mode, "empty") ? "" : "KDE Agent Native Fixture");
    if (strcmp(app_id_mode, "omitted")) xdg_toplevel_set_app_id(s->top, !strcmp(app_id_mode, "empty") ? "" : "org.kde_agent.fixture");
    if (parent) xdg_toplevel_set_parent(s->top, parent->top);
    xdg_toplevel_set_min_size(s->top, s->width, s->height);
    xdg_toplevel_set_max_size(s->top, s->width, s->height);
    surface_event("surface_created", s);
    printf(",\"parent\":"); if (parent) quoted(parent->label); else printf("null");
    printf(",\"title_mode\":"); quoted(title_mode); printf(",\"app_id_mode\":"); quoted(app_id_mode);
    printf(",\"width\":%d,\"height\":%d}\n", s->width, s->height);
    wl_surface_commit(s->wl);
}
static void start_window_child(void) {
    window_child = fork();
    if (window_child < 0) die("window child fork failed");
    if (!window_child) {
        /* Exec opens a fresh Wayland connection; inherited cgroup is unchanged. */
        close(wl_display_get_fd(display));
        char duration[24]; snprintf(duration, sizeof(duration), "%u", child_window_ms);
        execl("/proc/self/exe", "wayland-fixture", "--autonomous", "--child-surface",
              "--exit-after-ms", duration, "--title-mode", title_mode,
              "--app-id-mode", app_id_mode, (char *)NULL);
        _exit(127);
    }
    event("window_child_spawned"); printf(",\"child_pid\":%d,\"duration_ms\":%u}\n", window_child, child_window_ms);
}
int main(int argc, char **argv) {
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--autonomous")) { autonomous = true; continue; }
        if (!strcmp(argv[i], "--sibling")) { sibling_window = true; continue; }
        if (!strcmp(argv[i], "--dialog")) { dialog_window = true; continue; }
        if (!strcmp(argv[i], "--child-surface")) { child_surface = true; continue; }
        if (!strcmp(argv[i], "--help")) { usage(); return 0; }
        if (i + 1 >= argc) { usage(); return 2; }
        const char *option = argv[i], *value = argv[++i];
        if (!strcmp(option, "--window-delay-ms")) window_delay_ms = option_number(value, 86400000);
        else if (!strcmp(option, "--exit-after-ms")) { exit_after_ms = option_number(value, 86400000); timed_exit = true; }
        else if (!strcmp(option, "--exit-code")) exit_code = (int)option_number(value, 255);
        else if (!strcmp(option, "--descendant-ms")) { descendant_ms = option_number(value, 86400000); if (!descendant_ms) { usage(); return 2; } }
        else if (!strcmp(option, "--child-window-ms")) { child_window_ms = option_number(value, 86400000); if (!child_window_ms) { usage(); return 2; } }
        else if (!strcmp(option, "--close-delay-ms")) close_delay_ms = option_number(value, 86400000);
        else if (!strcmp(option, "--close-mode")) {
            if (strcmp(value, "normal") && strcmp(value, "refuse") && strcmp(value, "delay") && strcmp(value, "confirmation")) { usage(); return 2; }
            close_mode = value;
        }
        else if (!strcmp(option, "--resize-after-ms")) schedule_option(value, true);
        else if (!strcmp(option, "--destroy-after-ms")) schedule_option(value, false);
        else if (!strcmp(option, "--title-mode") || !strcmp(option, "--app-id-mode")) {
            if (strcmp(value, "normal") && strcmp(value, "empty") && strcmp(value, "omitted")) { usage(); return 2; }
            if (!strcmp(option, "--title-mode")) title_mode = value; else app_id_mode = value;
        }
        else { usage(); return 2; }
    }
    if ((strcmp(close_mode, "normal") && (!timed_exit || !exit_after_ms || exit_after_ms <= window_delay_ms)) ||
        (!strcmp(close_mode, "delay") ? !close_delay_ms : close_delay_ms != 0) ||
        (!strcmp(close_mode, "confirmation") && dialog_window)) { usage(); return 2; }
    for (unsigned i = 0; i < 3; ++i) {
        struct fixture_surface *s = &surfaces[i];
        if ((s->resize_scheduled || s->destroy_scheduled) &&
            (child_surface || (i == 1 && !sibling_window) || (i == 2 && !dialog_window))) { usage(); return 2; }
        if ((s->resize_scheduled && s->resize_after_ms < window_delay_ms) ||
            (s->destroy_scheduled && s->destroy_after_ms < window_delay_ms)) { usage(); return 2; }
    }
    if (child_surface) {
        if (!autonomous || !timed_exit || sibling_window || dialog_window || child_window_ms) { usage(); return 2; }
        surfaces[0].label = "child"; surfaces[0].role = "child"; surfaces[0].width = 400; surfaces[0].height = 240;
    }
    generation = getenv("HARNESS_GENERATION");
    if (!generation && autonomous) generation = "00000000000000000000000000000000";
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
    if (descendant_ms) start_descendant();
    if (child_window_ms) start_window_child();
    unsigned long long started = monotonic_ns();
    if (autonomous || window_delay_ms || timed_exit || descendant_ms) {
        event("started"); printf(",\"autonomous\":%s,\"window_delay_ms\":%u,\"timed_exit\":%s,\"exit_after_ms\":%u,\"exit_code\":%d,\"close_mode\":",
                                 autonomous ? "true" : "false", window_delay_ms, timed_exit ? "true" : "false", exit_after_ms, exit_code);
        quoted(close_mode); printf(",\"close_delay_ms\":%u}\n", close_delay_ms);
    }
    for (unsigned i = 0; i < 3; ++i) {
        struct fixture_surface *s = &surfaces[i];
        if (s->resize_scheduled || s->destroy_scheduled) {
            surface_event("surface_schedule", s);
            printf(",\"started_ns\":%llu,\"resize_after_ms\":", started);
            if (s->resize_scheduled) printf("%u", s->resize_after_ms); else printf("null");
            printf(",\"destroy_after_ms\":");
            if (s->destroy_scheduled) printf("%u", s->destroy_after_ms); else printf("null");
            puts("}");
        }
    }
    char input[128]; size_t used = 0;
    while (running) {
        unsigned long long elapsed_ms = (monotonic_ns() - started) / 1000000ULL;
        if (timed_exit && elapsed_ms >= exit_after_ms) {
            event("timed_exit"); printf(",\"exit_code\":%d}\n", exit_code); break;
        }
        if (!windows_created && elapsed_ms >= window_delay_ms) {
            create_window(&surfaces[0], NULL);
            if (sibling_window) create_window(&surfaces[1], NULL);
            if (dialog_window) create_window(&surfaces[2], &surfaces[0]);
            windows_created = true;
        }
        for (unsigned i = 0; i < 3; ++i) run_surface_schedule(&surfaces[i], elapsed_ms);
        unsigned long long now_ns = monotonic_ns();
        for (unsigned i = 0; i < 3; ++i) {
            struct fixture_surface *s = &surfaces[i];
            if (s->close_due_ns && now_ns >= s->close_due_ns) {
                s->close_due_ns = 0;
                close_surface(s, "delayed_compositor");
            }
        }
        if (!running) break;
        if (window_child > 0) {
            int status; pid_t reaped = waitpid(window_child, &status, WNOHANG);
            if (reaped == window_child) {
                event("window_child_exit"); printf(",\"child_pid\":%d,\"wait_status\":%d}\n", window_child, status);
                window_child = 0;
            } else if (reaped < 0 && errno != EINTR) die("window child reap failed");
        }
        int timeout = 100;
        if (!windows_created && window_delay_ms - elapsed_ms < (unsigned)timeout) timeout = (int)(window_delay_ms - elapsed_ms);
        if (timed_exit && exit_after_ms - elapsed_ms < (unsigned)timeout) timeout = (int)(exit_after_ms - elapsed_ms);
        for (unsigned i = 0; i < 3; ++i) {
            struct fixture_surface *s = &surfaces[i];
            if (s->resize_scheduled && !s->resize_done && s->resize_after_ms - elapsed_ms < (unsigned)timeout)
                timeout = (int)(s->resize_after_ms - elapsed_ms);
            if (s->destroy_scheduled && !s->destroy_done && s->destroy_after_ms - elapsed_ms < (unsigned)timeout)
                timeout = (int)(s->destroy_after_ms - elapsed_ms);
            if (s->close_due_ns) {
                unsigned long long remaining_ms = s->close_due_ns > now_ns ?
                    (s->close_due_ns - now_ns + 999999ULL) / 1000000ULL : 0;
                if (remaining_ms < (unsigned)timeout) timeout = (int)remaining_ms;
            }
        }
        if (wl_display_dispatch_pending(display) < 0) die("Wayland dispatch failed");
        if (wl_display_flush(display) < 0 && errno != EAGAIN) die("Wayland flush failed");
        struct pollfd fds[2] = {{wl_display_get_fd(display), POLLIN, 0}, {autonomous ? -1 : STDIN_FILENO, POLLIN, 0}};
        if (poll(fds, 2, timeout) < 0) { if (errno == EINTR) continue; die("poll failed"); }
        if (fds[0].revents & (POLLERR | POLLHUP)) die("Wayland disconnected");
        if ((fds[0].revents & POLLIN) && wl_display_dispatch(display) < 0) die("Wayland disconnected");
        if (fds[1].revents & (POLLERR | POLLHUP)) break;
        if (fds[1].revents & POLLIN) {
            ssize_t n = read(STDIN_FILENO, input + used, sizeof(input) - used - 1); if (n <= 0) break; used += n; input[used] = 0;
            char *newline;
            while ((newline = strchr(input, '\n'))) {
                *newline = 0;
                if (!strcmp(input, "close")) {
                    running = false;
                    for (unsigned i = 0; i < 3; ++i) close_surface(&surfaces[i], "control");
                }
                else if (!strncmp(input, "close ", 6)) {
                    bool found = false;
                    for (unsigned i = 0; i < 3; ++i) if (!strcmp(input + 6, surfaces[i].label) && surfaces[i].wl) {
                        close_surface(&surfaces[i], "control"); found = true; break;
                    }
                    if (!found) die("unknown open surface");
                }
                else if (!strcmp(input, "open sibling")) create_window(&surfaces[1], NULL);
                else if (!strcmp(input, "open dialog")) {
                    if (!strcmp(close_mode, "confirmation")) die("confirmation reserves dialog slot");
                    if (!surfaces[0].top) die("dialog requires primary");
                    create_window(&surfaces[2], &surfaces[0]);
                }
                else {
                    unsigned value; unsigned long request_control_id; char extra;
                    if (sscanf(input, "state %u %lu%c", &value, &request_control_id, &extra) != 2) die("invalid control command");
                    struct fixture_surface *s = &surfaces[0];
                    if (s->closed) die("primary surface closed");
                    if (s->pending || s->dirty) { surface_event("control_busy", s); printf(",\"control_id\":%lu}\n", request_control_id); }
                    else { s->state = value; s->control_id = request_control_id; s->source = "control"; s->dirty = true; render(s); }
                }
                size_t consumed = (size_t)(newline - input) + 1; memmove(input, input + consumed, used - consumed); used -= consumed; input[used] = 0;
            }
            if (used == sizeof(input) - 1) die("control message too long");
        }
    }
    for (unsigned i = 0; i < 3; ++i) close_surface(&surfaces[i], "shutdown");
    wl_display_flush(display);
    wl_display_disconnect(display);
    event("fixture_exit"); printf(",\"exit_code\":%d}\n", exit_code);
    return exit_code;
}
