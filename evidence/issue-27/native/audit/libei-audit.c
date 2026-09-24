#include <libei.h>
#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>
_Static_assert(__builtin_types_compatible_p(__typeof__(&ei_new_sender), struct ei * (*)(void *)), "ei_new_sender");
_Static_assert(__builtin_types_compatible_p(__typeof__(&ei_unref), struct ei * (*)(struct ei *)), "ei_unref");
_Static_assert(__builtin_types_compatible_p(__typeof__(&ei_configure_name), void (*)(struct ei *, const char *)), "ei_configure_name");
_Static_assert(__builtin_types_compatible_p(__typeof__(&ei_setup_backend_fd), int (*)(struct ei *, int)), "ei_setup_backend_fd");
_Static_assert(__builtin_types_compatible_p(__typeof__(&ei_get_fd), int (*)(struct ei *)), "ei_get_fd");
_Static_assert(__builtin_types_compatible_p(__typeof__(&ei_dispatch), void (*)(struct ei *)), "ei_dispatch");
_Static_assert(__builtin_types_compatible_p(__typeof__(&ei_get_event), struct ei_event * (*)(struct ei *)), "ei_get_event");
_Static_assert(__builtin_types_compatible_p(__typeof__(&ei_now), uint64_t (*)(struct ei *)), "ei_now");
_Static_assert(__builtin_types_compatible_p(__typeof__(&ei_event_get_type), enum ei_event_type (*)(struct ei_event *)), "ei_event_get_type");
_Static_assert(__builtin_types_compatible_p(__typeof__(&ei_event_get_seat), struct ei_seat * (*)(struct ei_event *)), "ei_event_get_seat");
_Static_assert(__builtin_types_compatible_p(__typeof__(&ei_event_get_device), struct ei_device * (*)(struct ei_event *)), "ei_event_get_device");
_Static_assert(__builtin_types_compatible_p(__typeof__(&ei_event_unref), struct ei_event * (*)(struct ei_event *)), "ei_event_unref");
_Static_assert(__builtin_types_compatible_p(__typeof__(&ei_seat_ref), struct ei_seat * (*)(struct ei_seat *)), "ei_seat_ref");
_Static_assert(__builtin_types_compatible_p(__typeof__(&ei_seat_unref), struct ei_seat * (*)(struct ei_seat *)), "ei_seat_unref");
_Static_assert(__builtin_types_compatible_p(__typeof__(&ei_seat_has_capability), bool (*)(struct ei_seat *, enum ei_device_capability)), "ei_seat_has_capability");
_Static_assert(__builtin_types_compatible_p(__typeof__(&ei_seat_bind_capabilities), void (*)(struct ei_seat *, ...)), "ei_seat_bind_capabilities");
_Static_assert(__builtin_types_compatible_p(__typeof__(&ei_seat_unbind_capabilities), void (*)(struct ei_seat *, ...)), "ei_seat_unbind_capabilities");
_Static_assert(__builtin_types_compatible_p(__typeof__(&ei_device_get_seat), struct ei_seat * (*)(struct ei_device *)), "ei_device_get_seat");
_Static_assert(__builtin_types_compatible_p(__typeof__(&ei_device_ref), struct ei_device * (*)(struct ei_device *)), "ei_device_ref");
_Static_assert(__builtin_types_compatible_p(__typeof__(&ei_device_unref), struct ei_device * (*)(struct ei_device *)), "ei_device_unref");
_Static_assert(__builtin_types_compatible_p(__typeof__(&ei_device_has_capability), bool (*)(struct ei_device *, enum ei_device_capability)), "ei_device_has_capability");
_Static_assert(__builtin_types_compatible_p(__typeof__(&ei_device_start_emulating), void (*)(struct ei_device *, uint32_t)), "ei_device_start_emulating");
_Static_assert(__builtin_types_compatible_p(__typeof__(&ei_device_stop_emulating), void (*)(struct ei_device *)), "ei_device_stop_emulating");
_Static_assert(__builtin_types_compatible_p(__typeof__(&ei_device_keyboard_key), void (*)(struct ei_device *, uint32_t, bool)), "ei_device_keyboard_key");
_Static_assert(__builtin_types_compatible_p(__typeof__(&ei_device_frame), void (*)(struct ei_device *, uint64_t)), "ei_device_frame");
_Static_assert(EI_EVENT_CONNECT == 1, "EI_EVENT_CONNECT");
_Static_assert(EI_EVENT_DISCONNECT == 2, "EI_EVENT_DISCONNECT");
_Static_assert(EI_EVENT_SEAT_ADDED == 3, "EI_EVENT_SEAT_ADDED");
_Static_assert(EI_EVENT_SEAT_REMOVED == 4, "EI_EVENT_SEAT_REMOVED");
_Static_assert(EI_EVENT_DEVICE_ADDED == 5, "EI_EVENT_DEVICE_ADDED");
_Static_assert(EI_EVENT_DEVICE_REMOVED == 6, "EI_EVENT_DEVICE_REMOVED");
_Static_assert(EI_EVENT_DEVICE_PAUSED == 7, "EI_EVENT_DEVICE_PAUSED");
_Static_assert(EI_EVENT_DEVICE_RESUMED == 8, "EI_EVENT_DEVICE_RESUMED");
_Static_assert(EI_DEVICE_CAP_KEYBOARD == 4, "EI_DEVICE_CAP_KEYBOARD");
_Static_assert(sizeof(struct ei *) == 8, "sizeof context");
_Static_assert(sizeof(struct ei_seat *) == 8, "sizeof seat");
_Static_assert(sizeof(struct ei_device *) == 8, "sizeof device");
_Static_assert(sizeof(struct ei_event *) == 8, "sizeof event");
_Static_assert(sizeof(void *) == 8, "sizeof opaque");
_Static_assert(sizeof(const char *) == 8, "sizeof string");
_Static_assert(sizeof(int) == 4, "sizeof int");
_Static_assert(sizeof(enum ei_device_capability) == 4, "sizeof cap");
_Static_assert(sizeof(enum ei_event_type) == 4, "sizeof event_type");
_Static_assert(sizeof(bool) == 1, "sizeof bool");
_Static_assert(sizeof(uint32_t) == 4, "sizeof u32");
_Static_assert(sizeof(uint64_t) == 8, "sizeof u64");
int main(void) { puts("header signatures, enum constants and ABI sizes passed"); return 0; }
