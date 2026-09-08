import { mountIslands } from '@/lib';

import { FrontDoor } from './front-door';

/* The logged-out surfaces: the front door (home.html, served as the 401 body
 * at "/") and, once ported, /login. Unlike /build/ this bundle is deliberately
 * NOT token-gated — its page is what an anonymous visitor sees, so a gated
 * bundle would leave the form dead. It is built by vite.public.config.js with
 * everything inlined, because it must not depend on a shared chunk under the
 * gated /build/ path. Keep it to what those pages already showed the world:
 * nothing in here may import anything that names an authenticated endpoint. */
mountIslands([['[jet-front-door]', FrontDoor]]);
