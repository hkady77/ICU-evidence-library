let connection;

function database() {
  if (!connection) {
    connection = new Promise((resolve, reject) => {
      const request = indexedDB.open("icu-evidence-personal", 1);
      request.onupgradeneeded = () => {
        request.result.createObjectStore("entries", { keyPath: "id" });
      };
      request.onsuccess = () => {
        const db = request.result;
        db.onversionchange = () => {
          db.close();
          connection = undefined;
        };
        resolve(db);
      };
      request.onerror = () => reject(request.error);
    }).catch((error) => {
      connection = undefined;
      throw error;
    });
  }
  return connection;
}

async function transaction(mode, perform) {
  const db = await database();
  return new Promise((resolve, reject) => {
    const tx = db.transaction("entries", mode);
    const request = perform(tx.objectStore("entries"));
    tx.oncomplete = () => resolve(request.result);
    tx.onabort = () => reject(tx.error || new Error("Storage operation cancelled"));
  });
}

export function readFolder() {
  return transaction("readonly", (store) => store.getAll());
}

export async function updateEntry(id, update) {
  const db = await database();
  return new Promise((resolve, reject) => {
    const tx = db.transaction("entries", "readwrite");
    const store = tx.objectStore("entries");
    const request = store.get(id);
    let entry;
    request.onsuccess = () => {
      entry = update(request.result);
      if (entry) store.put(entry);
      else store.delete(id);
    };
    tx.oncomplete = () => resolve(entry);
    tx.onabort = () => reject(tx.error || new Error("Storage operation cancelled"));
  });
}

export async function verifyPdf(blob) {
  const signature = await blob.slice(0, 5).text();
  if (signature !== "%PDF-") throw new Error("This file is not a PDF.");
  return blob.slice(0, blob.size, "application/pdf");
}
