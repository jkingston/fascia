#define _GNU_SOURCE
#include "xdg-shell-client-protocol.h"
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include <wayland-client.h>

static struct wl_display *display;
static struct wl_compositor *compositor;
static struct wl_shm *shm;
static struct xdg_wm_base *shell;
static struct wl_surface *surface;
static struct wl_buffer *buffer;
static volatile sig_atomic_t stopping;
static int draws;
static int touches;
static struct wl_touch *touch;
static void touch_down(void *data, struct wl_touch *device, uint32_t serial,
                       uint32_t time, struct wl_surface *target, int32_t id,
                       wl_fixed_t x, wl_fixed_t y) {
  (void)data;
  (void)device;
  (void)serial;
  (void)time;
  (void)target;
  (void)id;
  printf("TOUCH %d %d %d\n", ++touches, wl_fixed_to_int(x), wl_fixed_to_int(y));
  fflush(stdout);
}
static void touch_up(void *data, struct wl_touch *device, uint32_t serial,
                     uint32_t time, int32_t id) {
  (void)data;
  (void)device;
  (void)serial;
  (void)time;
  (void)id;
}
static void touch_motion(void *data, struct wl_touch *device, uint32_t time,
                         int32_t id, wl_fixed_t x, wl_fixed_t y) {
  (void)data;
  (void)device;
  (void)time;
  (void)id;
  (void)x;
  (void)y;
}
static void touch_frame(void *data, struct wl_touch *device) {
  (void)data;
  (void)device;
}
static const struct wl_touch_listener touch_listener = {.down = touch_down,
                                                        .up = touch_up,
                                                        .motion = touch_motion,
                                                        .frame = touch_frame,
                                                        .cancel = touch_frame};
static void capabilities(void *data, struct wl_seat *seat, uint32_t value) {
  (void)data;
  if ((value & WL_SEAT_CAPABILITY_TOUCH) && !touch) {
    touch = wl_seat_get_touch(seat);
    wl_touch_add_listener(touch, &touch_listener, NULL);
  }
}
static const struct wl_seat_listener seat_listener = {.capabilities =
                                                          capabilities};
static void stop(int sig) {
  (void)sig;
  stopping = 1;
}
static void ping(void *data, struct xdg_wm_base *base, uint32_t serial) {
  (void)data;
  xdg_wm_base_pong(base, serial);
}
static const struct xdg_wm_base_listener shell_listener = {ping};
static void global(void *data, struct wl_registry *registry, uint32_t name,
                   const char *interface, uint32_t version) {
  (void)data;
  (void)version;
  if (!strcmp(interface, "wl_compositor"))
    compositor = wl_registry_bind(registry, name, &wl_compositor_interface, 1);
  if (!strcmp(interface, "wl_seat")) {
    struct wl_seat *seat =
        wl_registry_bind(registry, name, &wl_seat_interface, 1);
    wl_seat_add_listener(seat, &seat_listener, NULL);
  }
  if (!strcmp(interface, "wl_shm"))
    shm = wl_registry_bind(registry, name, &wl_shm_interface, 1);
  if (!strcmp(interface, "xdg_wm_base")) {
    shell = wl_registry_bind(registry, name, &xdg_wm_base_interface, 1);
    xdg_wm_base_add_listener(shell, &shell_listener, NULL);
  }
}
static void removed(void *data, struct wl_registry *registry, uint32_t name) {
  (void)data;
  (void)registry;
  (void)name;
}
static const struct wl_registry_listener registry_listener = {global, removed};
static void configured(void *data, struct xdg_surface *xdg, uint32_t serial) {
  (void)data;
  xdg_surface_ack_configure(xdg, serial);
  wl_surface_attach(surface, buffer, 0, 0);
  wl_surface_damage(surface, 0, 0, 1280, 800);
  wl_surface_commit(surface);
  if (!draws++) {
    puts("CONFIGURED_AND_COMMITTED");
    fflush(stdout);
  }
}
static const struct xdg_surface_listener surface_listener = {configured};
static void sized(void *data, struct xdg_toplevel *top, int32_t width,
                  int32_t height, struct wl_array *states) {
  (void)data;
  (void)top;
  (void)width;
  (void)height;
  (void)states;
}
static void closed(void *data, struct xdg_toplevel *top) {
  (void)data;
  (void)top;
  stopping = 1;
}
static const struct xdg_toplevel_listener top_listener = {.configure = sized,
                                                          .close = closed};
int main(void) {
  signal(SIGTERM, stop);
  signal(SIGINT, stop);
  display = wl_display_connect(NULL);
  if (!display)
    return 2;
  struct wl_registry *registry = wl_display_get_registry(display);
  wl_registry_add_listener(registry, &registry_listener, NULL);
  if (wl_display_roundtrip(display) < 0 || !compositor || !shm || !shell)
    return 3;
  int fd = memfd_create("fascia-c-test", MFD_CLOEXEC);
  size_t size = 1280 * 800 * 4;
  if (fd < 0 || ftruncate(fd, size))
    return 4;
  uint32_t *pixels =
      mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
  if (pixels == MAP_FAILED)
    return 5;
  for (int y = 0; y < 800; y++)
    for (int x = 0; x < 1280; x++)
      pixels[y * 1280 + x] = 0xff000000u | ((uint32_t)(x * 255 / 1280) << 16) |
                             ((uint32_t)(y * 255 / 800) << 8) | 0x50;
  struct wl_shm_pool *pool = wl_shm_create_pool(shm, fd, size);
  buffer = wl_shm_pool_create_buffer(pool, 0, 1280, 800, 1280 * 4,
                                     WL_SHM_FORMAT_XRGB8888);
  wl_shm_pool_destroy(pool);
  close(fd);
  surface = wl_compositor_create_surface(compositor);
  struct xdg_surface *xdg = xdg_wm_base_get_xdg_surface(shell, surface);
  xdg_surface_add_listener(xdg, &surface_listener, NULL);
  struct xdg_toplevel *top = xdg_surface_get_toplevel(xdg);
  xdg_toplevel_add_listener(top, &top_listener, NULL);
  xdg_toplevel_set_title(top, "Fascia C interoperability test");
  xdg_toplevel_set_fullscreen(top, NULL);
  wl_surface_commit(surface);
  while (!stopping) {
    if (wl_display_dispatch_pending(display) < 0)
      break;
    wl_display_flush(display);
    struct pollfd item = {wl_display_get_fd(display), POLLIN, 0};
    if (poll(&item, 1, 100) > 0 && wl_display_dispatch(display) < 0)
      break;
  }
  const char *state = getenv("XDG_STATE_HOME");
  if (state) {
    char path[4096];
    int count = snprintf(path, sizeof(path), "%s/clean-exit", state);
    if (count > 0 && (size_t)count < sizeof(path)) {
      FILE *saved = fopen(path, "w");
      if (saved) {
        fprintf(saved, "%d\n", touches);
        fclose(saved);
      }
    }
  }
  puts("CLEAN_EXIT");
  fflush(stdout);
  wl_display_disconnect(display);
  munmap(pixels, size);
  return 0;
}
