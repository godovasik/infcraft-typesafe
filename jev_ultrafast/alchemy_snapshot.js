(() => {
  if (!document.body) return null;

  const cache = (window.__typeSafeAlchemy ||= {
    ids: new WeakMap(),
    nodes: new Map(),
    next: 1,
  });

  const identity = (prefix, element) => {
    let id = cache.ids.get(element);
    if (!id) {
      id = `${prefix}${cache.next++}`;
      cache.ids.set(element, id);
    }
    cache.nodes.set(id, element);
    return id;
  };

  for (const [id, element] of cache.nodes) {
    if (!element.isConnected) cache.nodes.delete(id);
  }

  const visible = (element) => {
    if (!element?.isConnected || element.closest('[aria-hidden="true"], [inert]')) return false;
    const style = getComputedStyle(element);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = element.getBoundingClientRect();
    return (
      rect.width > 0 &&
      rect.height > 0 &&
      rect.bottom > 0 &&
      rect.right > 0 &&
      rect.top < innerHeight &&
      rect.left < innerWidth
    );
  };

  const value = (element, selector, fallback = '') =>
    element?.querySelector(selector)?.textContent?.trim() || fallback;

  const item = (element) => {
    const name = element.getAttribute('data-item-text')?.trim() || '';
    const emoji = element.getAttribute('data-item-emoji')?.trim() || value(element, '.item-emoji');
    const id = identity('el_', element);
    return { id, name, emoji, label: `${emoji} ${name}`.trim() };
  };

  const instance = (element) => {
    const name = value(element, '.instance-text');
    const emoji = value(element, '.instance-emoji');
    return { id: identity('inst_', element), name, emoji, label: `${emoji} ${name}`.trim() };
  };

  const inventory = [...document.querySelectorAll('.item[data-item-text][data-item-id]')]
    .filter(visible)
    .map(item)
    .filter(({ name }) => name);
  const instances = [...document.querySelectorAll('#instances .instance')]
    .filter(visible)
    .map(instance)
    .filter(({ name }) => name);
  const board = document.querySelector('#instances');
  const drop_zone = board ? { id: identity('board_', board), label: 'Crafting board' } : null;
  const busy = Boolean(document.querySelector('#instances .instance-disabled, #instances .instance-pinwheel'));
  const semantic = { url: location.href, inventory, instances, drop_zone, busy };

  return {
    ...semantic,
    title: document.title,
    marker: JSON.stringify(semantic),
  };
})()
