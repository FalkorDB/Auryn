<?php

namespace App;

use App\Web\Main;

function bootstrap(): string
{
    $main = new Main();
    $main->run();

    return version();
}

function version(): string
{
    return '1.0.0';
}
