// Sfondo realistico per tutte le mappe del portale (Leaflet).
// Con la chiave Google (GETVINGO_GOOGLE_MAPS_KEY sul server): satellite Google con strade e nomi
// (Map Tiles API). Senza chiave: satellite Esri con strade e nomi dei luoghi sovrapposti.
window.GetvingoMappa = (function () {
  var CHIAVE = window.GETVINGO_GMAPS || '';
  var ESRI = 'https://server.arcgisonline.com/ArcGIS/rest/services/';

  function sessioneGoogle() {
    // la sessione delle tessere dura circa due settimane: si riusa finché non scade
    try {
      var s = JSON.parse(localStorage.getItem('gv-gmaps') || 'null');
      if (s && s.chiave === CHIAVE && s.scade > Date.now() + 3600e3) return Promise.resolve(s.session);
    } catch (e) {}
    return fetch('https://tile.googleapis.com/v1/createSession?key=' + encodeURIComponent(CHIAVE), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mapType: 'satellite', language: 'it-IT', region: 'IT', layerTypes: ['layerRoadmap'] })
    }).then(function (r) { if (!r.ok) throw new Error('sessione'); return r.json(); }).then(function (d) {
      try { localStorage.setItem('gv-gmaps', JSON.stringify({ chiave: CHIAVE, session: d.session, scade: Number(d.expiry) * 1000 })); } catch (e) {}
      return d.session;
    });
  }

  function esri(mappa) {
    var opz = { maxZoom: 20, maxNativeZoom: L.Browser.retina ? 18 : 19, detectRetina: true };
    L.tileLayer(ESRI + 'World_Imagery/MapServer/tile/{z}/{y}/{x}', Object.assign({ attribution: 'Tiles © Esri' }, opz)).addTo(mappa);
    // strade e nomi sopra il satellite: si riconosce dove si è
    L.tileLayer(ESRI + 'Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}', opz).addTo(mappa);
    L.tileLayer(ESRI + 'Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}', opz).addTo(mappa);
  }

  return {
    google: !!CHIAVE,
    chiave: CHIAVE,
    sfondo: function (mappa) {
      if (!CHIAVE) return esri(mappa);
      sessioneGoogle().then(function (s) {
        L.tileLayer('https://tile.googleapis.com/v1/2dtiles/{z}/{x}/{y}?session=' + s + '&key=' + encodeURIComponent(CHIAVE),
                    { maxZoom: 21, maxNativeZoom: 21, attribution: 'Immagini e dati © Google' }).addTo(mappa);
      }).catch(function () { esri(mappa); });  // chiave non valida o servizio non attivo: sfondo Esri
    }
  };
})();
