<?php

namespace App\Web;

use App\Models\Logger;
use App\Models\Store;

function make_widget(Logger $logger): Widget
{
    $store = new Store($logger);
    return new Widget($store);
}
