/* Nederlandse voorleesfunctie voor de leerlingentoets. Geen externe API. */
(() => {
  'use strict';
  const synth = window.speechSynthesis;
  const supported = !!(synth && window.SpeechSynthesisUtterance);
  const questionArea = document.getElementById('question');
  if (!questionArea) return;
  let speaking = false;
  let activeButton = null;
  let run = 0;
  const voiceKey = 'toets3k-voorleesstem';
  const voices = () => synth.getVoices().filter(v => /^nl([_-]|$)/i.test(v.lang));
  const bestVoice = () => {
    const list = voices();
    const stored = (() => { try { return sessionStorage.getItem(voiceKey); } catch { return null; } })();
    return list.find(v => v.voiceURI === stored) ||
      list.find(v => /natural|neural|premium|enhanced/i.test(v.name)) ||
      list.find(v => /^nl-NL$/i.test(v.lang) && v.localService) ||
      list.find(v => /^nl-NL$/i.test(v.lang)) || list[0] || null;
  };
  function reset() {
    speaking = false;
    if (activeButton) {
      activeButton.textContent = '🔊 Lees voor';
      activeButton.setAttribute('aria-pressed', 'false');
    }
    activeButton = null;
  }
  function stop() {
    run++;
    if (supported) synth.cancel();
    reset();
  }
  function readText() {
    const prompt = questionArea.querySelector('.question-text');
    const title = questionArea.querySelector('.question-heading h2');
    const notice = questionArea.querySelector('.notice');
    const choices = Array.from(questionArea.querySelectorAll('fieldset.choices'));
    const segments = [title?.textContent, prompt?.textContent, notice?.textContent].filter(Boolean);
    for (const group of choices) {
      const legend = group.querySelector('legend');
      if (legend) segments.push(legend.textContent);
      for (const label of group.querySelectorAll('label.choice span')) {
        segments.push(label.textContent);
      }
    }
    return segments.join('. ');
  }
  function start(button) {
    if (!supported) return;
    stop();
    const text = readText();
    if (!text.trim()) return;
    const myRun = ++run;
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = 'nl-NL';
    utterance.rate = 0.88;
    utterance.pitch = 1;
    const voice = bestVoice();
    if (voice) utterance.voice = voice;
    utterance.onend = utterance.onerror = () => { if (run === myRun) reset(); };
    activeButton = button;
    speaking = true;
    button.textContent = '■ Stop voorlezen';
    button.setAttribute('aria-pressed', 'true');
    synth.speak(utterance);
  }
  function controls() {
    if (!supported) return;
    const heading = questionArea.querySelector('.question-heading');
    if (!heading) return;
    const toolbar = document.createElement('div');
    toolbar.className = 'read-aloud-toolbar';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'read-aloud-button secondary';
    button.textContent = '🔊 Lees voor';
    button.setAttribute('aria-label', 'Lees de vraag en antwoordmogelijkheden voor');
    button.setAttribute('aria-pressed', 'false');
    button.addEventListener('click', () => speaking ? stop() : start(button));
    toolbar.append(button);
    const list = voices();
    if (list.length > 1) {
      const select = document.createElement('select');
      select.className = 'read-aloud-voice';
      select.setAttribute('aria-label', 'Kies Nederlandse voorleesstem');
      for (const voice of list) {
        const option = document.createElement('option');
        option.value = voice.voiceURI;
        option.textContent = voice.name;
        select.append(option);
      }
      select.value = bestVoice()?.voiceURI || list[0].voiceURI;
      select.addEventListener('change', () => {
        stop();
        try { sessionStorage.setItem(voiceKey, select.value); } catch {}
      });
      toolbar.append(select);
    }
    heading.insertAdjacentElement('afterend', toolbar);
  }
  // Vragen worden dynamisch door exam.js vervangen.
  const observer = new MutationObserver(() => {
    const heading = questionArea.querySelector('.question-heading');
    if (!heading || questionArea.querySelector('.read-aloud-toolbar')) return;
    stop();
    controls();
  });
  observer.observe(questionArea, {childList: true});
  controls();
  document.addEventListener('visibilitychange', () => { if (document.hidden) stop(); });
  window.addEventListener('pagehide', stop);
  // Stop ook bij het navigeren tussen vragen.
  document.addEventListener('click', e => {
    if (e.target.closest('#prev, #next, #navgrid, #submit-form')) stop();
  }, true);
})();
