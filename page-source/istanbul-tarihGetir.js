// Source: https://appointment.as-visa.com/PageJs/Macaristan/TR/istanbul/tarihGetir.js
// Fetched and saved for auto-book research

var dateDisabled = [];
$('#AppointmentTabID').change(function () {
    showLoading()
    var id = $('#AppointmentTabID').val();
    var cid = $('#NationalityTabID').val();
    var token = $('input[name="__RequestVerificationToken"]').val();
    $.ajax({
        url: '/Macaristan/TarihGetir',
        data: { tabId: id, countryid: cid },
        headers: {
            'RequestVerificationToken': token
        },
        type: 'Post',
        dataType: 'json',
        success: function (data) {
            hideLoading();
            window.dateDisabled = data;
        },
        error: function (xhr) {
            hideLoading();
            if (xhr.status === 403) {
                alert('Güvenlik doğrulaması başarısız. Lütfen sayfayı yenileyin.');
            } else {
                alert('Tarih bilgileri alınamadı. Lütfen tekrar deneyin.');
            }
        }
    })
})
