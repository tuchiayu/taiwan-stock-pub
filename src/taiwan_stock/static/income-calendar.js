// The server owns scope matching and totals; JS only enhances GET submission.
document.querySelectorAll('[data-income-scope]').forEach((select) => {
  select.form.querySelector('[data-income-scope-submit]').hidden = true;
  select.addEventListener('change', () => select.form.requestSubmit());
});

// Keep the ordinary month form usable when a native picker is unavailable.
document.querySelectorAll('[data-income-month]').forEach((input) => {
  if (input.type !== 'month' || typeof input.showPicker !== 'function') return;
  const form = input.form;
  const submit = form.querySelector('[data-income-month-submit]');
  form.classList.add('income-month-picker-enhanced');
  submit.hidden = true;
  const openPicker = () => {
    try { input.showPicker(); }
    catch (_) {
      form.classList.remove('income-month-picker-enhanced');
      submit.hidden = false;
      input.focus();
    }
  };
  input.addEventListener('click', openPicker);
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      openPicker();
    }
  });
  input.addEventListener('change', () => {
    if (input.value && input.validity.valid && form.classList.contains('income-month-picker-enhanced')) form.requestSubmit();
  });
});

// Native dialogs keep details out of the month view and support Escape/focus return.
document.querySelectorAll('[data-income-dialog]').forEach((trigger) => {
  trigger.addEventListener('click', () => {
    const dialog = document.getElementById(trigger.dataset.incomeDialog);
    if (!dialog) return;
    dialog.showModal();
  });
});

document.querySelectorAll('.income-dialog').forEach((dialog) => {
  dialog.addEventListener('click', (event) => {
    if (event.target !== dialog) return;
    const bounds = dialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right ||
        event.clientY < bounds.top || event.clientY > bounds.bottom) {
      dialog.close();
    }
  });
});
