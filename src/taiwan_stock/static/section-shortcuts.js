// Design 034: mark the shortcut whose section holds the line just below the sticky bars.
const nav = document.querySelector('[data-section-shortcuts]');
if (nav) {
  const header = document.querySelector('.site-header');
  const items = [...nav.querySelectorAll('a[href^="#"]')]
    .map((link) => {
      const target = document.getElementById(link.hash.slice(1));
      return target && { link, section: target.closest('section') || target };
    })
    .filter(Boolean);
  let current = null;
  let pending = false;
  // A shortcut or URL hash the user chose wins while its section is on screen, even
  // when the page cannot scroll far enough to bring that section to the line.
  let chosen = items.find(({ link }) => link.hash === location.hash) || null;

  const pick = () => {
    const barsBottom = Math.max(header ? header.getBoundingClientRect().bottom : 0, nav.getBoundingClientRect().bottom);
    if (chosen) {
      const box = chosen.section.getBoundingClientRect();
      // Kept while off screen: the first update runs before the browser jumps to the hash.
      if (box.top < innerHeight && box.bottom > barsBottom) return chosen.link;
    }
    // Anchor targets land 16px (scroll-margin) below the bars; look a little past that.
    const line = barsBottom + 32;
    let found = items.find(({ section }) => {
      const box = section.getBoundingClientRect();
      return box.top <= line && box.bottom > line;
    });
    // A short last section can never reach the line, so the page bottom selects it.
    const atBottom = scrollY > 0 && innerHeight + scrollY >= document.documentElement.scrollHeight - 2;
    if (atBottom) {
      const visible = items.filter(({ section }) => section.getBoundingClientRect().top < innerHeight);
      if (visible.length) found = visible[visible.length - 1];
    }
    return found ? found.link : null;
  };

  const update = () => {
    pending = false;
    const link = pick();
    if (link === current) return;
    if (current) current.removeAttribute('aria-current');
    current = link;
    if (!link) return;
    link.setAttribute('aria-current', 'location');
    // Scroll only the shortcut bar, never the page.
    const bar = nav.getBoundingClientRect();
    const box = link.getBoundingClientRect();
    if (box.left < bar.left) nav.scrollLeft -= bar.left - box.left + 4;
    else if (box.right > bar.right) nav.scrollLeft += box.right - bar.right + 4;
  };

  const schedule = () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(update);
  };
  nav.addEventListener('click', (event) => {
    chosen = items.find(({ link }) => link === event.target.closest('a')) || chosen;
    schedule();
  });
  const release = () => {
    chosen = null;
  };
  ['wheel', 'touchmove', 'keydown', 'pointerdown'].forEach((type) => {
    addEventListener(type, release, { passive: true });
  });
  addEventListener('scroll', schedule, { passive: true });
  addEventListener('resize', schedule);
  addEventListener('hashchange', schedule);
  document.addEventListener('toggle', schedule, true);
  update();
}
