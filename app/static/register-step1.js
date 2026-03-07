(function() {
    var cat = document.getElementById('category');
    var addrDiv = document.getElementById('address-fields');
    var addrInput = document.getElementById('address');
    var cszInput = document.getElementById('city_state_zip');
    function toggle() {
        var need = (cat.value === 'food' || cat.value === 'beverage');
        addrDiv.style.display = need ? '' : 'none';
        addrInput.required = need;
        cszInput.required = need;
    }
    cat.addEventListener('change', toggle);
    toggle();
})();
