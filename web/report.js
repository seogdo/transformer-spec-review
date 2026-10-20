"use strict";

// This document displays a saved review. Printing never changes its status,
// records a human approval, calls a model, or reports that a file was saved.
const printButton = document.getElementById("print-button");
const printError = document.getElementById("print-error");

if (printButton) {
  printButton.addEventListener("click", () => {
    if (printError) {
      printError.textContent = "";
      printError.hidden = true;
    }
    try {
      if (typeof window.print !== "function") throw new Error("Print is unavailable.");
      window.print();
      // A return or afterprint event cannot distinguish cancellation from a
      // completed print/PDF save. Do not claim success in either situation.
    } catch (_) {
      if (printError) {
        printError.textContent = "인쇄 창을 열지 못했습니다. 브라우저 메뉴의 인쇄 기능을 사용해 주세요. 파일 저장 완료 여부는 브라우저에서 직접 확인해 주세요.";
        printError.hidden = false;
      }
    }
  });
}
