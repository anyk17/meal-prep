// Shared auth store. Write clients call notifyIfUnauthenticated on every error response; it is
// harmless in the static build, where nothing writes and no login UI is mounted.
import { createAuthStore, isUnauthenticated } from './authModel.js';

export const authStore = createAuthStore();

export function notifyIfUnauthenticated(status, code) {
  if (isUnauthenticated(status, code)) authStore.dispatch({ type: 'expired' });
}
