// Spawn a child process without a visible console window on Windows GUI hosts.
// dsh can run inside a GUI host with no console, where Windows otherwise shows
// a black console window for every image generation call.
import { spawn } from 'node:child_process';

export function spawnHidden(command, args, options) {
  return spawn(command, args, { ...options, windowsHide: true });
}
