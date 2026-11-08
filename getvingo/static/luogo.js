// "Usa la mia posizione" nella configurazione di un luogo: coordinate dal GPS del telefono e, se vuoti,
// comune e indirizzo ricavati dal punto (OpenStreetMap). Si salva solo premendo il pulsante del modulo.
(function () {
  document.querySelectorAll('[data-usa-posizione]').forEach(function (b) {
    var modulo = b.closest('form');
    var esito = modulo.querySelector('[data-esito-posizione]');
    var campo = function (n) { return modulo.querySelector('[name=' + n + ']'); };
    var scrivi = function (testo, errore) {
      if (!esito) return;
      esito.textContent = testo; esito.hidden = false;
      esito.classList.toggle('errore', !!errore);
    };
    b.addEventListener('click', function () {
      if (!navigator.geolocation) return scrivi('Questo browser non dà la posizione.', true);
      b.classList.add('in-corso');
      scrivi('Rilevo la posizione…');
      navigator.geolocation.getCurrentPosition(function (p) {
        b.classList.remove('in-corso');
        campo('lat').value = p.coords.latitude.toFixed(6);
        campo('lon').value = p.coords.longitude.toFixed(6);
        var precisione = Math.round(p.coords.accuracy);
        scrivi('Posizione presa (precisione ±' + precisione + ' m)' +
               (precisione > 100 ? ': è poco precisa, riprova all’aperto.' : '.') + ' Cerco il comune…');
        fetch('https://nominatim.openstreetmap.org/reverse?format=jsonv2&addressdetails=1&zoom=18&accept-language=it&lat=' +
              p.coords.latitude + '&lon=' + p.coords.longitude)
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function (d) {
            var a = d && d.address;
            if (!a) throw 0;
            // in Italia il comune è "municipality"; village, town e hamlet sono spesso frazioni
            var comune = a.municipality || a.city || a.town || a.village || '';
            var prov = (a['ISO3166-2-lvl6'] || '').replace(/^IT-/, '');
            if (comune && !campo('comune').value) campo('comune').value = comune + (prov ? ' (' + prov + ')' : '');
            var via = [a.road, a.house_number].filter(Boolean).join(' ');
            var ind = campo('indirizzo');
            if (ind && via && !ind.value) ind.value = via + (a.postcode ? ', ' + a.postcode : '');
            scrivi('Posizione presa (±' + precisione + ' m)' + (comune ? ' · ' + comune : '') +
                   '. Controlla i dati e salva. (Comune da OpenStreetMap)');
          })
          .catch(function () { scrivi('Posizione presa (±' + precisione + ' m). Scrivi tu il comune, poi salva.'); });
      }, function (err) {
        b.classList.remove('in-corso');
        scrivi(err.code === 1 ? 'Hai negato l’accesso alla posizione: consentilo nelle impostazioni del browser e riprova.'
                              : 'Posizione non disponibile: attiva il GPS e riprova.', true);
      }, { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 });
    });
  });
})();
