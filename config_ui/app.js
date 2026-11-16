'use strict';

const $ = id => document.getElementById(id);
const workspace = $('workspace');
const dialog = $('editor');
const form = $('form');
const uploads = new Map();
const destinations = new WeakMap();
const purposes = ['завтрак', 'в школу'];
let config, revision, dirty = false, saving = false, tab = 'baskets', drag;

function node(tag, className, ...children) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  element.append(...children);
  return element;
}

function button(label, action, className = '', title = '') {
  const element = node('button', className, label);
  element.type = 'button';
  if (title) {
    element.title = title;
    element.setAttribute('aria-label', title);
  }
  element.onclick = action;
  return element;
}

function status(text, error = false) {
  $('status').textContent = text;
  $('status').classList.toggle('error', error);
}

function changed() {
  dirty = true;
  status('Есть несохранённые изменения');
  render();
}

async function request(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Редактор не получил ответ.');
  return data;
}

function categories() {
  const names = new Map();
  for (const name of [...config.categories, ...config.baskets.map(basket => basket.category)]) {
    if (!names.has(name.toLowerCase())) names.set(name.toLowerCase(), name);
  }
  return [...names.values()];
}

function sameCategory(left, right) {
  return left.toLowerCase() === right.toLowerCase();
}

function destination(element, type, list, item = null, assign = {}) {
  destinations.set(element, {type, list, item, assign});
  return element;
}

function endZone(type, list, assign, label) {
  return destination(node('div', 'drop-end', label), type, list, null, assign);
}

function grip(type, list, item, card, label) {
  const handle = button('⠿', null, 'grip', `Перетащить: ${label}`);
  handle.onpointerdown = event => {
    if (!event.isPrimary || event.button !== 0 || saving) return;
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    drag = {type, list, item, card, x: event.clientX, y: event.clientY, label};
  };
  handle.onpointermove = event => {
    if (!drag || !handle.hasPointerCapture(event.pointerId)) return;
    if (!drag.ghost && Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < 5) return;
    drag.x = event.clientX;
    drag.y = event.clientY;
    if (!drag.ghost) {
      drag.ghost = node('div', 'drag-ghost', drag.label);
      document.body.append(drag.ghost);
      document.body.classList.add('dragging');
      drag.card.classList.add('drag-source');
      drag.frame = requestAnimationFrame(moveGhost);
    }
  };
  handle.onpointerup = event => {
    if (!drag || !handle.hasPointerCapture(event.pointerId)) return;
    drag.x = event.clientX;
    drag.y = event.clientY;
    if (drag.ghost) locateDrop();
    finishDrag(true);
  };
  handle.onlostpointercapture = () => finishDrag(false);
  handle.onkeydown = event => {
    if (saving || !['ArrowUp', 'ArrowDown'].includes(event.key)) return;
    event.preventDefault();
    const index = list.indexOf(item);
    const next = index + (event.key === 'ArrowUp' ? -1 : 1);
    if (next < 0 || next >= list.length) return;
    [list[index], list[next]] = [list[next], list[index]];
    if (type === 'category') config.categories = list;
    changed();
    [...workspace.querySelectorAll('.grip')].find(el => el.title === handle.title)?.focus();
  };
  return handle;
}

function locateDrop() {
  drag.target?.element.classList.remove('drop-active', 'drop-before', 'drop-after');
  drag.target = null;
  for (let element = document.elementFromPoint(drag.x, drag.y); element; element = element.parentElement) {
    const target = destinations.get(element);
    if (!target || target.type !== drag.type) continue;
    if (target.item === drag.item) return;
    const rect = element.getBoundingClientRect();
    const after = drag.y >= rect.top + rect.height / 2;
    element.classList.add(target.item == null ? 'drop-active' : after ? 'drop-after' : 'drop-before');
    drag.target = {element, ...target, after};
    return;
  }
}

function moveGhost() {
  if (!drag?.ghost) return;
  drag.ghost.style.transform = `translate(${drag.x + 14}px, ${drag.y + 12}px)`;
  if (drag.y > innerHeight - 65) window.scrollBy(0, 14);
  if (drag.y < 95) window.scrollBy(0, -14);
  locateDrop();
  drag.frame = requestAnimationFrame(moveGhost);
}

function finishDrag(apply) {
  if (!drag) return;
  const {item, type, list, target, card, ghost, frame} = drag;
  drag = null;
  cancelAnimationFrame(frame);
  ghost?.remove();
  document.body.classList.remove('dragging');
  card.classList.remove('drag-source');
  target?.element.classList.remove('drop-active', 'drop-before', 'drop-after');
  if (!apply || !target) return;
  if (type === 'option' && target.list !== list && target.list.some(option => option.sku === item.sku)) {
    status('В этой группе уже есть вариант с таким артикулом.', true);
    return;
  }
  list.splice(list.indexOf(item), 1);
  const index = target.item == null ? target.list.length : target.list.indexOf(target.item) + Number(target.after);
  target.list.splice(index, 0, item);
  if (typeof item === 'object') Object.assign(item, target.assign);
  if (type === 'category') config.categories = target.list;
  changed();
}

document.addEventListener('keydown', event => {
  if (event.key === 'Escape') finishDrag(false);
});

function thumbnail(ref, className = 'thumb') {
  if (!ref) return node('span', className);
  const image = node('img', className);
  image.alt = '';
  image.loading = 'lazy';
  image.src = uploads.get(ref) || `/api/photo?ref=${encodeURIComponent(ref)}`;
  image.onerror = () => image.replaceWith(node('span', className));
  return image;
}

const textField = (name, label, required = true) => ({name, label, required, type: 'text'});
const selectField = (name, label, choices) => ({name, label, choices});
const numberField = (name, label) => ({name, label, type: 'number', required: true});
const photoField = {name: 'photo', label: 'Фотография JPG или PNG', type: 'file'};
const nameField = textField('name', 'Название');

function formControl(spec, value) {
  const control = node(spec.choices ? 'select' : 'input');
  control.name = spec.name;
  control.required = Boolean(spec.required);
  if (spec.choices) {
    for (const [key, label] of spec.choices) {
      const option = node('option', '', label);
      option.value = key;
      control.append(option);
    }
  } else {
    control.type = spec.type;
    if (spec.type === 'text') control.maxLength = spec.maxLength || 100;
    if (spec.type === 'number') { control.min = 1; control.max = 12; }
    if (spec.type === 'file') control.accept = 'image/jpeg,image/png';
  }
  if (spec.type !== 'file') control.value = Array.isArray(value) ? value.join(', ') : value ?? '';
  return control;
}

function fileData(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error('Не удалось прочитать фотографию.'));
    reader.readAsDataURL(file);
  });
}

function edit(title, record, specs, apply, remove) {
  if (saving) return;
  $('editor-title').textContent = title;
  const fields = $('fields');
  fields.replaceChildren();
  const error = node('p', 'meta');
  let clearPhoto = false;
  for (const spec of specs) {
    const control = formControl(spec, record[spec.name]);
    const field = node('label', spec.type === 'file' ? 'photo-drop' : '', spec.label, control);
    fields.append(field);
    if (spec.type === 'file') {
      field.append(node('small', 'meta', 'Можно перетащить файл сюда'));
      field.ondragover = event => { event.preventDefault(); field.classList.add('drop-active'); };
      field.ondragleave = () => field.classList.remove('drop-active');
      field.ondrop = event => {
        event.preventDefault();
        field.classList.remove('drop-active');
        if (event.dataTransfer.files.length === 1) control.files = event.dataTransfer.files;
        else error.textContent = 'Перетащите одну фотографию.';
      };
    }
    if (spec.type === 'file' && record.photo) {
      const current = node('div', 'item', thumbnail(record.photo, 'photo-preview'),
        button('Убрать фото', () => { clearPhoto = true; current.remove(); }, 'edit'));
      fields.append(current);
    }
  }
  fields.append(error);
  $('remove').hidden = !remove;
  $('remove').onclick = () => {
    try {
      if (window.confirm('Удалить эту запись?')) { remove(); dialog.close(); changed(); }
    } catch (failure) { error.textContent = failure.message; }
  };
  form.onsubmit = async event => {
    event.preventDefault();
    const submit = form.querySelector('[type=submit]');
    submit.disabled = true;
    try {
      const data = new FormData(form);
      const values = {};
      for (const spec of specs) {
        const value = data.get(spec.name);
        if (spec.type === 'file') {
          values.photo = clearPhoto ? null : record.photo || null;
          if (value.size) {
            if (!['image/jpeg', 'image/png'].includes(value.type) || value.size > 10_000_000) {
              throw new Error('Выберите JPG или PNG размером до 10 МБ.');
            }
            const key = `pending:${crypto.randomUUID()}`;
            uploads.set(key, await fileData(value));
            values.photo = key;
          }
        } else if (spec.type === 'number') values[spec.name] = Number(value);
        else if (spec.list) values[spec.name] = value.split(',').map(part => part.trim()).filter(Boolean);
        else values[spec.name] = value.trim() || null;
        if (spec.required && values[spec.name] === null) throw new Error(`Заполните поле «${spec.label}».`);
      }
      apply(values);
      dialog.close();
      changed();
    } catch (failure) { error.textContent = failure.message; }
    finally { submit.disabled = false; }
  };
  dialog.showModal();
}

function deleteItem(list, record) {
  list.splice(list.indexOf(record), 1);
}

function editCategory(name) {
  edit(name ? 'Категория' : 'Новая категория', {name}, [nameField], values => {
    const names = categories();
    if (names.some(other => other !== name && sameCategory(other, values.name))) {
      throw new Error('Категория с таким названием уже есть.');
    }
    config.categories = name ? names.map(other => other === name ? values.name : other) : [...names, values.name];
    if (name) for (const basket of config.baskets) {
      if (sameCategory(basket.category, name)) basket.category = values.name;
    }
  }, name && !config.baskets.some(basket => sameCategory(basket.category, name))
    ? () => { config.categories = categories().filter(other => other !== name); } : null);
}

function editBasket(category, basket) {
  const data = {name: basket?.name || '', category};
  edit(basket ? 'Корзина' : 'Новая корзина', data,
    [nameField, selectField('category', 'Категория', categories().map(name => [name, name]))], values => {
      if (basket) Object.assign(basket, values);
      else config.baskets.push({id: `basket-${crypto.randomUUID()}`, ...values, groups: []});
    }, basket ? () => deleteItem(config.baskets, basket) : null);
}

function editGroup(basket, group) {
  edit(group ? 'Группа товаров' : 'Новая группа товаров', group || {pick: 1, strategy: 'ordered'}, [
    nameField, numberField('pick', 'Сколько разных вариантов взять'),
    selectField('strategy', 'Выбор вариантов', [['ordered', 'По порядку'], ['random', 'Случайно']]),
  ], values => {
    if (group) Object.assign(group, values);
    else basket.groups.push({...values, options: []});
  }, group ? () => deleteItem(basket.groups, group) : null);
}

function editOption(group, option) {
  edit(option ? 'Товар в корзине' : 'Новый товар в корзине', option || {}, [
    nameField, textField('sku', 'Артикул «Магнита»'), textField('query', 'Поисковый запрос', false), photoField,
  ], values => {
    if (!/^\d+$/.test(values.sku)) throw new Error('Артикул должен состоять из цифр.');
    if (group.options.some(other => other !== option && other.sku === values.sku)) throw new Error('Артикул уже есть в группе.');
    if (option) Object.assign(option, values);
    else group.options.push(values);
  }, option ? () => deleteItem(group.options, option) : null);
}

function editProduct(purpose, product) {
  const listField = (name, label) => ({...textField(name, label, false), list: true, maxLength: 600});
  edit(product ? 'Обычный продукт' : 'Новый обычный продукт', product || {purpose, quantity: 1}, [
    nameField, numberField('quantity', 'Количество упаковок'),
    selectField('purpose', 'Назначение', purposes.map(value => [value, value])),
    listField('required', 'Обязательные слова через запятую'),
    listField('exclude', 'Исключения через запятую'),
    listField('replacements', 'ID замен через запятую'), photoField,
  ], values => {
    if (product) Object.assign(product, values);
    else config.products.push({id: `product-${crypto.randomUUID().slice(0, 8)}`, ...values});
  }, product ? () => {
    if (config.products.some(other => other.replacements.includes(product.id))) {
      throw new Error('Сначала уберите этот ID из списков замен других продуктов.');
    }
    deleteItem(config.products, product);
  } : null);
}

function productCard(type, list, item, assign, subtitle, open) {
  const card = node('div', 'item');
  card.append(grip(type, list, item, card, item.name), thumbnail(item.photo),
    node('div', 'item-info', item.name, node('small', '', subtitle)), button('✎', open, 'edit', `Редактировать: ${item.name}`));
  return destination(card, type, list, item, assign);
}

function groupCard(basket, group) {
  const card = node('section', 'group');
  card.append(node('div', 'group-head', grip('group', basket.groups, group, card, group.name),
    node('strong', '', group.name, node('div', 'meta', `${group.pick} из ${group.options.length} · ${group.strategy === 'random' ? 'случайно' : 'по порядку'}`)),
    button('✎', () => editGroup(basket, group), 'edit', `Редактировать группу: ${group.name}`)));
  for (const option of group.options) {
    card.append(productCard('option', group.options, option, {}, `Артикул ${option.sku}`, () => editOption(group, option)));
  }
  card.append(endZone('option', group.options, {}, 'Перенести товар сюда'),
    button('＋ Товар', () => editOption(group), 'add'));
  return destination(card, 'group', basket.groups, group);
}

function basketCard(category, basket) {
  const card = node('article', 'basket');
  card.append(node('div', 'basket-head', grip('basket', config.baskets, basket, card, basket.name),
    node('h3', '', basket.name), button('✎', () => editBasket(category, basket), 'edit', `Редактировать корзину: ${basket.name}`)));
  for (const group of basket.groups) card.append(groupCard(basket, group));
  card.append(endZone('group', basket.groups, {}, 'Перенести группу сюда'), button('＋ Группа', () => editGroup(basket), 'add'));
  return destination(card, 'basket', config.baskets, basket, {category});
}

function renderBaskets() {
  const names = categories();
  if (!names.length) workspace.append(node('p', 'empty', 'Создайте категорию, чтобы добавить первую корзину.'));
  for (const name of names) {
    const section = node('section', 'category');
    const baskets = config.baskets.filter(basket => sameCategory(basket.category, name));
    section.append(node('div', 'section-head', grip('category', names, name, section, name),
      node('h2', '', name, node('span', 'count', String(baskets.length))),
      button('✎', () => editCategory(name), 'edit', `Редактировать категорию: ${name}`),
      button('＋ Корзина', () => editBasket(name), 'outline')));
    if (baskets.length) section.append(node('div', 'basket-grid', ...baskets.map(basket => basketCard(name, basket))));
    else section.append(node('p', 'empty', 'Здесь пока нет корзин.'));
    section.append(endZone('basket', config.baskets, {category: name}, 'Перенести корзину в эту категорию'));
    workspace.append(destination(section, 'category', names, name));
  }
}

function renderProducts() {
  const grid = node('div', 'ordinary-grid');
  for (const purpose of purposes) {
    const lane = node('section', 'ordinary-lane', node('h2', '', purpose === 'завтрак' ? 'Завтрак' : 'В школу'));
    for (const product of config.products.filter(item => item.purpose === purpose)) {
      lane.append(productCard('product', config.products, product, {purpose},
        `${product.quantity} шт. · ${product.id}`, () => editProduct(purpose, product)));
    }
    lane.append(endZone('product', config.products, {purpose}, 'Перенести продукт сюда'),
      button('＋ Продукт', () => editProduct(purpose), 'add'));
    grid.append(lane);
  }
  workspace.append(grid);
}

function renderSettings() {
  const address = formControl(textField('address', 'Адрес доставки'), config.address);
  const backend = formControl(selectField('backend', 'Режим сборки', [['api', 'API'], ['playwright', 'Playwright']]), config.backend);
  for (const control of [address, backend]) {
    control.oninput = () => {
      config[control.name] = control.value;
      dirty = true;
      $('save').disabled = false;
      status('Есть несохранённые изменения');
    };
  }
  workspace.append(node('div', 'settings', node('label', '', 'Адрес доставки', address),
    node('label', '', 'Режим сборки', backend)));
}

function render() {
  workspace.replaceChildren();
  $('save').disabled = !dirty || saving;
  $('create-category').hidden = !config || tab !== 'baskets';
  const selected = document.querySelector(`[data-tab="${tab}"]`);
  $('title').textContent = selected.textContent;
  $('intro').hidden = tab === 'settings';
  for (const control of document.querySelectorAll('[data-tab]')) {
    if (control === selected) control.setAttribute('aria-current', 'page');
    else control.removeAttribute('aria-current');
  }
  if (!config) return;
  if (tab === 'baskets') renderBaskets();
  else if (tab === 'products') renderProducts();
  else renderSettings();
}

for (const control of document.querySelectorAll('[data-tab]')) {
  control.onclick = () => { if (!saving) { tab = control.dataset.tab; render(); } };
}
$('create-category').onclick = () => editCategory();
$('close').onclick = () => dialog.close();
window.addEventListener('beforeunload', event => { if (dirty) event.preventDefault(); });

async function save() {
  if (!dirty || saving) return;
  saving = true;
  $('save').disabled = true;
  workspace.inert = true;
  status('Сохраняю…');
  try {
    const records = [...config.products, ...config.baskets.flatMap(basket => basket.groups.flatMap(group => group.options))];
    const photos = Object.fromEntries(records.filter(record => uploads.has(record.photo))
      .map(record => [record.photo, uploads.get(record.photo).split(',')[1]]));
    const data = await request('/api/config', {method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({revision, settings: config, uploads: photos})});
    config = data.settings;
    revision = data.revision;
    uploads.clear();
    dirty = false;
    status('Изменения сохранены');
  } catch (error) { status(error.message, true); }
  finally { saving = false; workspace.inert = false; render(); }
}

$('save').onclick = save;
request('/api/config').then(data => {
  config = data.settings;
  revision = data.revision;
  status('');
  render();
}).catch(error => status(error.message, true));
