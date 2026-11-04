// Frame hook used by services/capture.py. Do not remove.
//
// seek(t) freezes every CSS animation on the page at time t (in seconds) so the renderer can take
// a screenshot of that exact moment. A slide may define window.onSeek(t) to update things that are
// not pure CSS, such as the countdown number.
window.seek = function (t) {
    document.getAnimations().forEach(function (animation) {
        animation.pause();
        animation.currentTime = t * 1000;
    });
    if (typeof window.onSeek === "function") {
        window.onSeek(t);
    }
};
